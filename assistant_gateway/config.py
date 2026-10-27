"""Config configuration and contracts."""

from __future__ import annotations
import logging
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Mapping, Optional, cast
import yaml
from pydantic import BaseModel, Field, PrivateAttr, field_validator, model_validator
from config_backup_retention import prune_config_backups
from config_file_updates import config_revision, replace_config_file
from storage.backends import (
    STORAGE_BACKEND_POSTGRES,
    STORAGE_BACKEND_POSTGRESQL_ALIAS,
    STORAGE_BACKEND_SQLITE,
)
from custom_messages.contracts.runtime_v1 import (
    DELIVERY_CHANNEL_DISCORD,
    DELIVERY_CHANNEL_TELEGRAM,
    MEM_REVIEW_AGENT_TYPE,
)
from default_values import (
    MAX_STORAGE_WARNING_STEP_GB,
    ACTIVITY_COLLECTOR_DEFAULTS,
    AGENT_DEFAULTS,
    DELIVERY_DEFAULTS,
    GATEWAY_DEFAULTS,
    HEARTBEAT_DEFAULTS,
    LAUNCH_DEFAULTS,
    STORAGE_DEFAULTS,
)
from env_secrets import get_env_with_file_fallback
from storage.postgres_connection import with_bundled_postgres_address
from runtime_settings.secrets_source import (
    SECRETS_SOURCE_ENVIRONMENT,
    SecretsSource,
    normalize_secrets_source,
)
from id_generation import (
    HEARTBEAT_SCHEDULE_ID_PREFIX,
    HEARTBEAT_SOURCE_ID_PREFIX,
    operational_id,
)
from assistant_gateway.interactive_apps.identifiers import (
    BUILTIN_INTERNAL_WEBSITE_PLUGIN_ID,
)
from storage.config import (
    DEFAULT_SQLITE_STORAGE_PATH,
    StorageSettings,
    resolve_storage_settings,
)
from runtime_settings.agent_types import AgentTypeRuntimeSettings
from runtime_settings.access_workflows import AccessWorkflowSettings
from runtime_settings.agents import AgentRuntimeDefaultsSettings
from runtime_settings.instance_layout import InstanceLayout
from runtime_settings.legacy_layout import (
    drop_removed_location_settings,
    present_removed_location_settings,
)
from runtime_settings.appearance import UISettings
from runtime_settings.common import _normalized_optional_string
from runtime_settings.execution import ExecutionControlSettings
from runtime_settings.worker_pools import move_worker_capacity_settings
from runtime_settings.governance import (
    AuthorizationSettings,
    ToolPolicySettings,
    refuse_removed_access_settings,
)
from runtime_settings.interactive_apps import (
    AdapterHostSettings,
    INTERACTIVE_APP_EXECUTION_IN_PROCESS,
    InteractiveAppsSettings,
)
from runtime_settings.mcp import MCPSettings
from runtime_settings.models import InstanceEmbeddingSettings, InstanceLLMSettings
from runtime_settings.network import (
    NetworkSettings,
    compose_public_base_url,
    compose_secure_tool_broker_public_base_url,
)
from runtime_settings.security import SecurityLayerSettings
from runtime_settings.services import WebServicesSettings
from runtime_settings.storage import (
    ManagerPostgresStorageSettings,
    ManagerSqliteStorageSettings,
    ManagerStorageSettings,
    StatePostgresStorageSettings,
    StateSqliteStorageSettings,
    StateStorageSettings,
)
from runtime_settings.telemetry import ActivityCollectorSettings, OTelSettings
from runtime_settings.transport import TransportSettings
from runtime_settings.voice import VoiceSettings
from runtime_settings.workspace import WorkspaceSettings
from secure_tools.chat_commands import ACCOUNTS_MODEL_SUBJECT, ACCOUNTS_PRIVATE, ACCOUNTS_SHARE, accounts_command


logger = logging.getLogger(__name__)


DEFAULT_INSTANCE_ROOT = Path.home() / GATEWAY_DEFAULTS.instance_root_name.value


INSTANCE_CONFIG_SCHEMA_VERSION = 12


DIRECTLY_COMPATIBLE_INSTANCE_CONFIG_VERSIONS = {INSTANCE_CONFIG_SCHEMA_VERSION}


DEFAULT_UNAVAILABLE_MESSAGE_TEMPLATE = (
    GATEWAY_DEFAULTS.unavailable_message_template.value
)


DEFAULT_AVAILABLE_MESSAGE_TEMPLATE = GATEWAY_DEFAULTS.available_message_template.value


DEFAULT_MANAGER_TRANSITION_NOTICES_ENABLED = (
    GATEWAY_DEFAULTS.manager_transition_notices_enabled.value
)


@dataclass(frozen=True)
class InstanceConfigVersionStatus:
    path: Path
    detected_version: int
    latest_version: int
    directly_compatible_versions: tuple[int, ...]
    needs_migration: bool
    is_directly_compatible: bool


@dataclass(frozen=True)
class InstanceConfigMigrationResult:
    path: Path
    original_version: int
    migrated_version: int
    backup_path: Optional[Path]
    applied_steps: tuple[str, ...]


def _resolve_schedule_db_path(
    raw_path: Optional[str], *, instance_root_path: Path
) -> Optional[str]:
    """The task database path, relative paths resolved against the instance root."""

    if not raw_path:
        return None
    schedule_path = Path(raw_path).expanduser()
    if not schedule_path.is_absolute():
        schedule_path = (instance_root_path / schedule_path).resolve()
    else:
        schedule_path = schedule_path.resolve()
    return str(schedule_path)


def _migrate_instance_to_layout(migrated: Dict[str, Any]) -> None:
    """Schema 10: every location is derived from ``instance_root``.

    The old single ``<root>/workspace`` folder is replaced by ``control/``,
    ``dbs/``, ``state/`` and ``workspaces/`` (runtime_settings.instance_layout).
    Settings that held the default locations are dropped; a customized location
    stops the upgrade, because its files must be moved by hand. Files are never
    moved here: startup refuses while old-layout files remain and prints the
    commands (runtime_settings.legacy_layout).
    """

    root = Path(str(migrated.get("instance_root") or ".")).expanduser().resolve()
    legacy_workspace = root / "workspace"
    agent = dict(migrated.get("agent") or {})
    raw_workspace = str(agent.pop("workspace_dir", "") or "").strip()
    if raw_workspace and Path(raw_workspace).expanduser().resolve() != legacy_workspace:
        raise ValueError(
            f"agent.workspace_dir is set to {raw_workspace}, which is not the default "
            f"{legacy_workspace}. Locations are now derived from instance_root: move "
            f"the files from {raw_workspace} into {legacy_workspace}, remove "
            "agent.workspace_dir, and upgrade again; startup then prints the exact moves."
        )
    migrated["agent"] = agent
    drop_removed_location_settings(migrated)
    state = dict(migrated.get("state") or {})
    storage = dict(state.get("storage") or {})
    sqlite = dict(storage.get("sqlite") or {})
    raw_db = str(sqlite.get("path") or "").strip()
    db_path = Path(raw_db).expanduser() if raw_db else None
    # Relative paths used to resolve inside the workspace and now resolve from
    # the root, so "dbs/task_history.db" keeps working once the file is moved.
    if db_path is not None and db_path.is_absolute():
        resolved_db = db_path.resolve()
        if resolved_db == legacy_workspace / "dbs" / "task_history.db":
            sqlite["path"] = "dbs/task_history.db"
        elif legacy_workspace in resolved_db.parents:
            raise ValueError(
                f"state.storage.sqlite.path ({raw_db}) is inside the old workspace, which "
                "agents could reach. Move the database to "
                f"{root / 'dbs'}, update the setting, and upgrade again."
            )
        storage["sqlite"] = sqlite
        state["storage"] = storage
        migrated["state"] = state


def _resolve_instance_path(
    raw_path: Optional[str], *, instance_root_path: Path
) -> Optional[str]:
    if not raw_path:
        return None
    target_path = Path(raw_path).expanduser()
    if not target_path.is_absolute():
        target_path = (instance_root_path / target_path).resolve()
    else:
        target_path = target_path.resolve()
    return str(target_path)


def _is_path_within(root: Path, candidate: Path) -> bool:
    try:
        candidate.relative_to(root)
        return True
    except ValueError:
        return False


def _drop_none_values(value: Any) -> Any:
    """Remove nested None values before writing operator-facing YAML."""

    if isinstance(value, dict):
        return {
            key: _drop_none_values(child)
            for key, child in value.items()
            if child is not None
        }
    if isinstance(value, list):
        return [_drop_none_values(child) for child in value if child is not None]
    return value


_EXPLICIT_NULL_PUBLIC_PORT_PATHS = (
    ("security_layer", "broker_public_port"),
    (
        "interactive_apps",
        "plugins",
        BUILTIN_INTERNAL_WEBSITE_PLUGIN_ID,
        "settings",
        "public_port",
    ),
)


_MISSING_CONFIG_VALUE = object()
# Replaced by the team's model-key sharing (access_control.account_sharing);
# GatewayUsersSettings refuses it and the onboarding import lists it as removed.
REMOVED_TEAM_MODEL_KEYS_SETTING: tuple[str, ...] = ("gateway", "users", "new_people_use_team_model_keys")


def _nested_config_value(data: Mapping[str, Any], path: tuple[str, ...]) -> Any:
    current: Any = data
    for key in path:
        if not isinstance(current, Mapping) or key not in current:
            return _MISSING_CONFIG_VALUE
        current = current[key]
    return current


def _set_nested_config_value(
    data: Dict[str, Any], path: tuple[str, ...], value: Any
) -> None:
    target = data
    for key in path[:-1]:
        child = target.get(key)
        if not isinstance(child, dict):
            child = {}
            target[key] = child
        target = child
    target[path[-1]] = value


def _drop_incidental_config_nulls(data: Dict[str, Any]) -> Dict[str, Any]:
    """Keep explicit public-port and model-catalog nulls while dropping empty defaults."""

    sanitized = cast(Dict[str, Any], _drop_none_values(data))
    for path in _EXPLICIT_NULL_PUBLIC_PORT_PATHS:
        if _nested_config_value(data, path) is None:
            _set_nested_config_value(sanitized, path, None)
    # A model catalog's null request option disables an inherited setting.
    # Dropping it would change the model request after a settings save.
    model_paths: list[tuple[str, ...]] = [("llm", "model_capabilities")]
    model_paths.extend(
        ("agent_types", agent_type, "llm", "model_capabilities")
        for agent_type in data.get("agent_types") or {}
    )
    for model_path in model_paths:
        catalog = _nested_config_value(data, model_path)
        if isinstance(catalog, dict):
            _set_nested_config_value(sanitized, model_path, catalog)
    return sanitized


def _strip_persisted_instance_config_secrets(data: Dict[str, Any]) -> Dict[str, Any]:
    """Remove secrets and secret-like runtime credentials before writing YAML."""
    sanitized = dict(data)

    transport = dict(sanitized.get("transport") or {})
    transport.pop("nats_password", None)
    if transport:
        sanitized["transport"] = transport

    gateway = dict(sanitized.get("gateway") or {})
    gateway_storage = dict(gateway.get("storage") or {})
    gateway_postgres = dict(gateway_storage.get(STORAGE_BACKEND_POSTGRES) or {})
    gateway_postgres.pop("dsn", None)
    if gateway_storage:
        gateway_storage[STORAGE_BACKEND_POSTGRES] = gateway_postgres
        gateway["storage"] = gateway_storage
        sanitized["gateway"] = gateway

    state = dict(sanitized.get("state") or {})
    state_storage = dict(state.get("storage") or {})
    state_postgres = dict(state_storage.get(STORAGE_BACKEND_POSTGRES) or {})
    state_postgres.pop("dsn", None)
    if state_storage:
        state_storage[STORAGE_BACKEND_POSTGRES] = state_postgres
        state["storage"] = state_storage
        sanitized["state"] = state

    activity_collector = dict(sanitized.get("activity_collector") or {})
    activity_collector.pop("user_token", None)
    activity_collector.pop("admin_token", None)
    activity_collector.pop("ingest_token", None)
    activity_storage = dict(activity_collector.get("storage") or {})
    activity_postgres = dict(activity_storage.get(STORAGE_BACKEND_POSTGRES) or {})
    activity_postgres.pop("dsn", None)
    if activity_storage:
        activity_storage[STORAGE_BACKEND_POSTGRES] = activity_postgres
        activity_collector["storage"] = activity_storage
    if activity_collector:
        sanitized["activity_collector"] = activity_collector

    interactive_apps = dict(sanitized.get("interactive_apps") or {})
    discord = dict(interactive_apps.get(DELIVERY_CHANNEL_DISCORD) or {})
    discord.pop("bot_token_env_var", None)
    if discord:
        interactive_apps[DELIVERY_CHANNEL_DISCORD] = discord
    telegram = dict(interactive_apps.get(DELIVERY_CHANNEL_TELEGRAM) or {})
    telegram.pop("bot_token_env_var", None)
    if telegram:
        interactive_apps[DELIVERY_CHANNEL_TELEGRAM] = telegram
    if interactive_apps:
        sanitized["interactive_apps"] = interactive_apps

    return sanitized


_INSTANCE_CONFIG_COMMENT_COLUMNS = {
    0: 38,
    2: 44,
    4: 50,
    6: 56,
    8: 62,
    10: 68,
    12: 74,
}


_INSTANCE_CONFIG_COMMENTS: Dict[str, str] = {
    "schema_version": "Instance config schema version; generated by onboarding and migration tools.",
    "instance_name": "Stable instance name used for ids, display defaults, and generated file paths.",
    "instance_root": "Instance root. control/ (team control files), dbs/, state/ (runtime records) and workspaces/ (users/, team/, agents/) are derived from it; relative instance paths resolve here.",
    "launch": "Container and process launch settings.",
    "launch.mode": "Startup mode for the bundled container.",
    "launch.env_file_path": "Runtime secrets file. Relative paths resolve under instance_root. Empty when secrets_source is environment.",
    "launch.secrets_source": "Where secrets come from: env_file (team.env) or environment (environment variables and Docker secrets only; no team.env is written or read).",
    "launch.restart_on_complete": "Whether web onboarding asks the container to restart after finalization.",
    "network": "Browser-facing network origin and advertised service ports.",
    "network.public_origin": "Browser-facing http(s) origin without a path; service ports are appended separately.",
    "transport": "NATS/JetStream messaging settings. NATS is the permanent internal transport.",
    "transport.type": "NATS/JetStream is the only supported transport.",
    "transport.nats_url": "Single-server URL.",
    "transport.storage_dir": "Transport-owned local storage directory. Relative paths resolve under instance_root.",
    "transport.nats_servers": "NATS server URLs.",
    "transport.jetstream_enabled": "Use JetStream for durable work/reply/presence messages.",
    "transport.subject_prefix": "Team subject prefix. Empty uses team_id, e.g. team_303. Use per-team values for external NATS ACLs.",
    "transport.storage": 'JetStream storage: "file" or "memory".',
    "transport.replicas": "JetStream stream replica count.",
    "transport.ack_wait_seconds": "Delivery ack wait for long-running agent work.",
    "transport.max_deliver": "Maximum redelivery attempts before termination.",
    "transport.duplicate_window_seconds": "JetStream duplicate message-id window.",
    "transport.work_max_age_seconds": "Durable work/reply retention age.",
    "transport.presence_max_age_seconds": "Replayable presence retention age.",
    "transport.process_consumer_inactive_threshold_seconds": "Expiry delay for unbound process-scoped JetStream consumers.",
    "mcp": "Installed MCP connection, transport, authorization, and managed-host settings.",
    "mcp.enabled": "Run the brokered MCP control/data plane before the first dynamic proposal exists.",
    "mcp.protocol_adapter": "Protocol adapter seam; use mcp_v1 until Strands supports MCP v2.",
    "mcp.load_retry_window_minutes": "Minutes an agent keeps retrying an installed MCP server that does not answer (after 1, 2, 4, 8, 16, then every 30 minutes) before it stops and tells administrators in the Inbox; 1 to 10080.",
    "mcp.static_reconciliation.upgrade_mode": "Existing version-4 deployments are grandfathered exactly once; new definitions require review.",
    "mcp.network_policy.allow_loopback": "Permit approved MCP endpoints that resolve to loopback addresses.",
    "mcp.network_policy.allow_private_networks": "Permit approved MCP endpoints on private or link-local networks.",
    "mcp.network_policy.allowed_private_hosts": "Exact hostnames allowed to resolve to private addresses while other private targets remain denied.",
    "mcp.legacy_sse.enabled": "Rare compatibility transport; Streamable HTTP is the default.",
    "mcp.legacy_sse.removal_target_date": "Operator reminder for removing legacy SSE support.",
    "mcp.hosting.enabled": "Enable reviewed declarative MCP hosting; arbitrary agent-authored code remains forbidden.",
    "gateway": "Gateway routing, Web Chat, request waiting, and gateway-local state settings.",
    "gateway.team_id": "Team id used for routing and subject-prefix defaults.",
    "gateway.named_agent_type": "Manager agent type the gateway targets.",
    "gateway.manager_agent_id": "Optional direct manager agent id. Blank uses live manager presence discovery.",
    "gateway.request_timeout_seconds": "Seconds to wait for one terminal manager reply before timing out locally.",
    "gateway.response_queue_name": "Gateway response queue name for manager replies.",
    "gateway.startup_agents": "Specialist agent processes supervised at headless startup.",
    "gateway.startup_agents.named_agent_type": "Agent type to supervise at headless startup.",
    "gateway.startup_agents.count": "Number of workers of this agent type to supervise.",
    "gateway.startup_agents.enabled": "Whether this startup agent entry is active.",
    "gateway.storage": "Gateway-local session, conversation, delivery, and pending-response state store.",
    "gateway.storage.backend": "Gateway-local state backend: sqlite or postgres.",
    "gateway.storage.sqlite": "SQLite settings for gateway-local state.",
    "gateway.storage.sqlite.path": "Gateway-local SQLite DB. Relative paths resolve under instance_root.",
    "gateway.storage.postgres": "Postgres settings for gateway-local state; connection strings stay in the environment.",
    "gateway.storage.postgres.schema_name": "Postgres schema for gateway-local state tables.",
    "gateway.users": "Gateway user registration, link-code, and web-token settings.",
    "gateway.users.enabled": "Enable gateway user records and identity linking.",
    "gateway.users.allow_newuser_registration": "Allow new user registration from supported app surfaces.",
    "gateway.users.link_code_ttl_seconds": "Link-code lifetime in seconds.",
    "gateway.users.web_user_invite_ttl_seconds": "One-time Web Chat invitation lifetime in seconds.",
    "gateway.users.web_user_tokens_enabled": "Enable browser user tokens for authenticated Web Chat sessions.",
    "gateway.users.web_user_token_ttl_days": "Browser user-token lifetime in days. 0 disables automatic expiration.",
    "gateway.users.removed_data_retention_days": "Days a removed person's folder, personal memory, own knowledge pages and conversations are held for /user restore before a maintenance run deletes them. 0 deletes them at the next maintenance run.",
    "gateway.storage_warnings": "When people hear that the data the team stores for them has grown, and when administrators hear that storage is filling up. Administrators can also change these in Settings > Team.",
    "gateway.storage_warnings.person_step_gb": "Each person gets one Inbox notice when their stored data (chats, completed tasks, their files and folder) passes each multiple of this many GB (1 GB = 1,000,000,000 bytes); the gateway measures it daily. 0 turns these notices off.",
    "gateway.storage_warnings.nearing_limit_percent": "The Status page warns, and administrators get one Inbox notice, when a disk the instance uses or the message server's storage reaches this percent of its limit.",
    "gateway.storage_warnings.nearly_full_percent": "At this percent of its limit the Status page reports a problem and administrators get another notice. Must be above nearing_limit_percent.",
    "state": "Manager, task-history, schedule, and heartbeat state settings.",
    "state.storage": "Manager/task-history/schedule persistence store.",
    "state.storage.backend": "Manager store backend: sqlite or postgres.",
    "state.storage.sqlite": "SQLite settings for manager/task-history/schedule data.",
    "state.storage.sqlite.path": "Manager/task-history/schedule SQLite DB. Relative paths resolve under instance_root; keep it outside workspaces/.",
    "state.storage.sqlite.vector": "SQLite vector-search settings for memory graph and related retrieval tables.",
    "state.storage.sqlite.vector.enabled": "Whether SQLite vector indexing is enabled.",
    "state.storage.sqlite.vector.provider": "SQLite vector provider used by fresh vector-enabled stores.",
    "state.storage.sqlite.vector.require_extension": "Fail memory graph startup if sqlite-vec cannot load.",
    "state.storage.sqlite.vector.embedding_dimensions": "Embedding dimension count for vector tables.",
    "state.storage.sqlite.vector.distance_metric": "Distance metric used for vector search.",
    "state.storage.sqlite.vector.migration_mode": "How vector schema migrations are applied.",
    "state.storage.postgres": "Postgres settings for manager data; connection strings stay in the environment.",
    "state.storage.postgres.schema_name": "Postgres schema for manager/task-history/schedule tables.",
    "execution_control": "Immutable code, schedule, service, Kubernetes, and SSH execution settings.",
    "execution_control.enabled": "Enable the deterministic execution control plane.",
    "execution_control.runner": "Fenced batch runner and service reconciler settings.",
    "execution_control.runner.enabled": "Start execution controllers in headless mode.",
    "execution_control.runner.tick_interval_seconds": "Seconds between schedule materialization and run claims.",
    "execution_control.runner.max_concurrent_jobs": "Maximum concurrent finite run attempts.",
    "execution_control.runner.lease_ttl_seconds": "Lease lifetime renewed while work remains active.",
    "execution_control.runner.service_reconcile_interval_seconds": "Seconds between managed-service health reconciliation.",
    "execution_control.artifact_store": "Immutable artifact storage limits.",
    "execution_control.artifact_store.maximum_artifact_bytes": "Maximum accepted artifact size.",
    "execution_control.profiles": "Operator-authored exact build and execution profiles.",
    "execution_control.kubernetes": "Kubernetes adapter settings.",
    "execution_control.kubernetes.kubectl_executable": "Operator-installed kubectl binary path or name.",
    "execution_control.kubernetes.network_policy_enforced": "Assert that execution namespaces enforce profile network labels.",
    "execution_control.ssh": "Interactive SSH PTY broker settings.",
    "execution_control.ssh.enabled": "Enable brokered interactive SSH sessions.",
    "execution_control.ssh.ssh_executable": "OpenSSH client binary path or name.",
    "execution_control.ssh.lease_ttl_seconds": "Live PTY lease lifetime renewed on interaction.",
    "execution_control.ssh.transcript_limit_bytes": "Maximum captured remote-output transcript bytes.",
    "execution_control.ssh.targets": "Operator-owned pinned SSH targets and broker-only key-file references.",
    "web_services": "Internal web research services the agents' tools call; the Crawl4AI token is a linked account (/link http:crawl4ai team), never a setting.",
    "web_services.crawl4ai": "This instance's Crawl4AI server; the screenshot tool reaches it through the secure tool broker.",
    "web_services.crawl4ai.base_url": "Internal Crawl4AI HTTP(S) URL; do not embed credentials. The broker sends the linked token only to this exact address.",
    "web_services.crawl4ai.timeout_seconds": "Per-request Crawl4AI timeout; the broker stops a request after 120 seconds at most.",
    "web_services.searxng": "SearXNG metasearch service connection and result defaults.",
    "web_services.searxng.base_url": "Internal SearXNG HTTP(S) URL; do not embed credentials.",
    "web_services.searxng.verify_tls": "Verify SearXNG TLS certificates for HTTPS connections.",
    "web_services.searxng.timeout_seconds": "Per-request SearXNG HTTP timeout.",
    "web_services.searxng.default_num_results": "Default bounded result count returned inline to an agent.",
    "agent": "Agent runtime defaults and workspace location.",
    "agent.memory_graph_enabled": "Enable graph-backed memory infrastructure.",
    "agent.memory": "Pinned-memory hydration, review, and notification settings.",
    "agent.memory.plugin_manager": "Memory plugin coordination before conversation-thread budgeting.",
    "agent.memory.plugin_manager.enabled": "Coordinate memory hydration before conversation-thread budgeting.",
    "agent.memory.plugin_manager.registry_mode": "Built-in repo-owned plugin registry; no arbitrary external loading.",
    "agent.memory.plugin_manager.fail_open": "Omit failed optional plugins and warn instead of failing the turn.",
    "agent.memory.plugin_manager.emit_plugin_warnings": "Include model-visible warnings for failed pinned memory plugins.",
    "agent.memory.plugins": "Memory hydration plugin order and per-plugin priority settings.",
    "agent.memory.plugins.order": "Hydration plugin order before priorities.",
    "agent.memory.plugins.enabled": "Enable this memory plugin.",
    "agent.memory.plugins.priority": "Memory plugin rendering priority.",
    "agent.memory.plugins.pinned": "Pin this memory plugin output during context budgeting.",
    "agent.memory.plugins.degradable": "Allow this memory plugin output to be omitted during context pressure.",
    "agent.memory.coremem": "Team-scoped pinned memory commands, tools, and hydration settings.",
    "agent.memory.coremem.enabled": "Enable team-scoped pinned memory commands/tools/hydration.",
    "agent.memory.coremem.max_facts_per_team": "Hard active coremem capacity per team.",
    "agent.memory.coremem.max_metadata_chars": "Maximum serialized metadata JSON length per coremem fact.",
    "agent.memory.coremem.max_tags": "Maximum tag count per coremem fact.",
    "agent.memory.coremem.max_tag_chars": "Maximum normalized tag length per coremem tag.",
    "agent.memory.coremem.hydrate_into_context": "Hydrate active coremem into every applicable turn.",
    "agent.memory.coremem.automatic_creation_enabled": "Allow MemReview to automatically create/reconcile coremem facts.",
    "agent.memory.coremem.agent_write_tools_enabled": "Allow agent tools to write coremem; slash commands still work when false.",
    "agent.memory.usermem": "Canonical-user-scoped pinned memory commands, tools, and hydration settings.",
    "agent.memory.usermem.enabled": "Enable canonical-user-scoped pinned memory commands/tools/hydration.",
    "agent.memory.usermem.max_facts_per_user": "Hard active usermem capacity per canonical user.",
    "agent.memory.usermem.require_canonical_user_id": "Skip usermem hydration when the requester is not registered.",
    "agent.memory.usermem.max_metadata_chars": "Maximum serialized metadata JSON length per usermem fact.",
    "agent.memory.usermem.max_tags": "Maximum tag count per usermem fact.",
    "agent.memory.usermem.max_tag_chars": "Maximum normalized tag length per usermem tag.",
    "agent.memory.usermem.hydrate_into_context": "Hydrate active usermem into every applicable registered-user turn.",
    "agent.memory.usermem.automatic_creation_enabled": "Allow MemReview to automatically create/reconcile usermem facts.",
    "agent.memory.usermem.agent_write_tools_enabled": "Allow agent tools to write usermem; slash commands still work when false.",
    "agent.memory.review": "Memory review maintenance boundaries and MemReview job settings.",
    "agent.memory.review.enabled": "Enable memory review maintenance boundaries and MemReview job enqueueing.",
    "agent.memory.review.mode": "Review scheduling policy for pinned-memory review jobs.",
    "agent.memory.review.periodic_turn_interval": "Maximum unreviewed completed user turns before submitting a conversation review window.",
    "agent.memory.review.sliding_window_overlap_turns": "Already-reviewed turns included before the first unreviewed turn for review continuity.",
    "agent.memory.review.explicit_cue_immediate": 'Allow explicit "remember" style cues to submit a partial window before the interval fills.',
    "agent.memory.review.max_turn_text_chars": "Per-turn text field cap inside MemReview conversation-window payloads.",
    "agent.memory.review.stale_conversation_after_seconds": "Inactivity age before remaining unreviewed turns are considered stale.",
    "agent.memory.review.max_stale_conversations_per_run": "Maximum stale conversations submitted per scheduled stale-review scan (each person's scan covers only their own conversations).",
    "agent.memory.review.stale_conversation_review_interval_seconds": "Recurring scan interval for stale conversations with unreviewed turns; one schedule per active person, owned by them.",
    "agent.memory.review.coremem_audit_interval_seconds": "Recurring interval for scheduled coremem validity/conflict audits; one team schedule.",
    "agent.memory.review.usermem_audit_interval_seconds": "Recurring interval for scheduled usermem validity/conflict audits; one schedule per active person, owned by them.",
    "agent.memory.review.claim_ttl_seconds": "Review-job claim TTL used by MemReview workers.",
    "agent.memory.review.memreview_agent_count": "Default managed MemReview worker count.",
    "agent.memory.review.review_task_ttl_seconds": "MemReview task NATS TTL in seconds.",
    "agent.memory.notifications": "Memory and topic notice delivery settings.",
    "agent.memory.notifications.quiet_hours_aware": "Route memory/topic notices through quiet-hours-aware gateway delivery.",
    "agent.memory.notifications.active_engagement_grace_seconds": "Grace window for treating a source conversation as actively engaged.",
    "agent.memory.notifications.delivery_target": "notify_user target used for memory/topic notices: default or current_thread.",
    "agent.memory.domain": "Tagged-thread topic recall and topic command settings.",
    "agent.memory.domain.enabled": "Enable tagged-thread topic recall and topic command surfaces.",
    "agent.memory.domain.auto_tag_threads": "Allow maintenance plugins to tag threads from explicit topic signals.",
    "agent.memory.domain.auto_tag_turns": "Allow maintenance plugins to tag individual turns from explicit topic signals.",
    "agent.memory.domain.visible_notices": "Allow user-visible topic/memory notices where delivery policy permits.",
    "agent.memory.domain.max_recall_threads": "Maximum compact thread hints inserted by domain_topic_recall.",
    "agent.memory.domain.max_tags_per_thread": "Maximum topic tags retained per conversation thread.",
    "agent.memory.domain.max_tags_per_turn": "Maximum topic tags retained per conversation turn.",
    "agent.memory.domain.allow_switch_by_tag": "Allow topic commands to switch active context by tag.",
    "agent.knowledge": "The shared knowledge base agents search and write: pages in the team space and each person's own space, with revisions, notes and reviewed team contributions.",
    "agent.knowledge.enabled": "Open the knowledge base and give agents its tools; when false the tools say it is not available.",
    "agent.knowledge.limits": "Per-agent write limits the store checks on every agent write, so no agent dominates the knowledge base or rewrites pages in a loop; people's edits are not rate limited.",
    "agent.knowledge.limits.window_minutes": "Window for the per-page edit limit and the edit-war check.",
    "agent.knowledge.limits.page_edits_per_agent": "Edits one agent may make to the same page within the window.",
    "agent.knowledge.limits.writes_per_agent_per_hour": "Knowledge writes (pages, notes, contributions, archives) one agent may make per hour.",
    "agent.knowledge.limits.new_pages_per_work": "New pages one person's request may create, counting every agent working on it.",
    "agent.knowledge.limits.archives_per_agent_per_hour": "Applied or proposed page archives one agent may make per hour.",
    "agent.knowledge.limits.recent_revisions_compared": "An edit whose text equals the current page or one of this many earlier revisions is refused.",
    "agent.knowledge.limits.edit_war_edits": "More than this many edits within the window from at least two agents freezes the page for agents.",
    "agent.knowledge.limits.edit_war_freeze_minutes": "How long a frozen page refuses agent edits; agents add notes instead.",
    "agent.knowledge.limits.max_title_chars": "Longest page title.",
    "agent.knowledge.limits.max_summary_chars": "Longest page summary or change description.",
    "agent.knowledge.limits.max_body_chars": "Longest page text; longer pages should be split into linked pages.",
    "agent.knowledge.limits.max_note_chars": "Longest note.",
    "agent.knowledge.limits.max_tags": "Most tags on one page.",
    "agent.knowledge.sensitive_content": "Content the knowledge base never stores, beyond the built-in rules (credentials and identity numbers).",
    "agent.knowledge.sensitive_content.blocked_patterns": "Team-specific regular expressions refused in every space, each with a short name shown in refusals, for example internal code names.",
    "agent.knowledge.curation": "KnowledgeCuratorAgent's schedules, installed by the gateway: one run per active person reviews the team changes proposed from their work; the team run tidies team pages. A run with nothing to review is skipped without starting the curator.",
    "agent.knowledge.curation.enabled": "Install the curator's schedules (also off when agent.knowledge.enabled is false).",
    "agent.knowledge.curation.person_interval_minutes": "Minutes between each person's curation runs; 0 installs none. Applies to new installs.",
    "agent.knowledge.curation.team_interval_minutes": "Minutes between team curation runs; 0 installs none. Applies to new installs.",
    "agent.knowledge.search": "Knowledge search settings.",
    "agent.knowledge.search.max_results": "Most results one knowledge search returns.",
    "agent.schedule_maintenance": "Manager-owned recurring internal-maintenance schedule settings.",
    "agent.schedule_maintenance.memory_review_cleanup": "Set abandoned review claims aside for InternalImprovementAgent; archive and deletion remain permission-checked.",
    "agent.schedule_maintenance.memory_review_cleanup.enabled": "Enable the abandoned memory-review claim scan.",
    "agent.schedule_maintenance.memory_review_cleanup.scan_interval_seconds": "Seconds between abandoned-claim scans.",
    "agent.schedule_maintenance.memory_review_cleanup.expired_claim_grace_seconds": "Additional seconds after claim expiry before setting an abandoned review aside.",
    "agent.schedule_maintenance.memory_review_cleanup.batch_size": "Maximum reviews handled per scan or authorized maintenance operation.",
    "agent.schedule_maintenance.memory_review_cleanup.archive_retention_days": "Minimum days to keep archived review content before authorized deletion.",
    "agent.schedule_maintenance.stale_schedules": "Before each maintenance run, check every schedule: delete removed people's own schedules and tell owners and administrators about ones that look unused.",
    "agent.schedule_maintenance.stale_schedules.enabled": "Enable the stale-schedule check (it runs only when the maintenance schedule does).",
    "agent.schedule_maintenance.stale_schedules.turned_off_days": "Tell a person about their schedule once it has been turned off for at least this many days.",
    "agent.schedule_maintenance.stale_schedules.paused_beyond_days": "Tell a person about their schedule when it is paused until more than this many days from now.",
    "agent.schedule_maintenance.stuck_tasks": "On every scheduler pass, the team lead finds stuck tasks and tells the person each works for (Inbox and chat), and again when it moves.",
    "agent.schedule_maintenance.stuck_tasks.enabled": "Tell people about stuck tasks.",
    "agent.schedule_maintenance.stuck_tasks.owner_gone_after_seconds": "Seconds after the agent holding a task stopped reporting in, with nobody taking it over, before the person is told. An agent without a worker start record that reports no process, or whose process the runtime cannot check, also counts as stopped after this long, so another agent may take its work over.",
    "agent.schedule_maintenance.stuck_tasks.waiting_on_person_after_seconds": "Seconds a task may wait on an access decision or the person's answer before they are reminded.",
    "agent.schedule_maintenance.stuck_tasks.no_progress_after_seconds": "Seconds an in-progress task may show no new history before the person is told.",
    "agent.schedule_maintenance.stuck_tasks.handback_unclaimed_after_seconds": "Seconds a task handed back by a stopped worker may wait for another agent before the person and administrators are told.",
    "agent.schedule_workers": "Automatic scheduled-worker eligibility, timing ceilings and editable defaults; saved default or TeamLead policies authorize individual schedules. How many workers may run is agent.worker_pools.",
    "agent.schedule_workers.eligible_agent_types": "TeamLead-managed automatic-start candidates; registered custom specialists may be added through runtime settings. Operator denies still win.",
    "agent.schedule_workers.default_startup_policy_enabled": "Give schedules for specialist types that ship with Universal Agents a default startup policy. Team-written types need eligible_agent_types. Grants no tool or credential access.",
    "agent.schedule_workers.defaults": "TeamLead-editable launch rules within the current operator ceilings.",
    "agent.worker_pools": "Worker pools: trusted code starts workers while delegated tasks wait and no worker is free, up to each type's pool size; one limit covers delegated and scheduled work.",
    "agent.worker_pools.enabled": "Start workers automatically for waiting delegated tasks.",
    "agent.worker_pools.default_max_workers_per_type": "Pool size of each worker type until TeamArchitect sets another (inside the ceiling).",
    "agent.worker_pools.max_workers_per_type_ceiling": "Operator ceiling for one worker type; TeamArchitect cannot set a pool above it.",
    "agent.worker_pools.max_total_workers_per_team": "Operator ceiling for all workers of the team, however they were started.",
    "agent.worker_pools.max_launches_per_type_per_hour": "Operator limit on worker starts per type per hour.",
    "agent.worker_pools.scale_up_wait_seconds": "How long a task waits with no free worker before another worker starts (a type with no worker starts one at once).",
    "agent.worker_pools.max_starts_per_pass": "Most workers started in one controller pass.",
    "agent.worker_pools.capacity_warning_ratio": "Share of a pool or team limit at which TeamArchitect gets a capacity review while tasks wait.",
    "agent.worker_pools.review_repeat_after_minutes": "A problem that persists this long after TeamArchitect's last review of it is reviewed again.",
    "agent.worker_pools.recurring_incident_count": "Repeats of the same problem within the window that TeamArchitect must report to administrators (who also get an Inbox notice).",
    "agent.worker_pools.recurring_incident_window_hours": "Window for counting repeats of a worker pool problem.",
    "agent.worker_pools.max_retries_after_worker_loss": "Times a delegated task moves to another worker after its worker was lost before it fails and its requester decides.",
    "agent.worker_pools.provider_limit": "What a worker does while its model provider refuses calls (rate limit, quota, overload).",
    "agent.worker_pools.provider_limit.cooldown_seconds": "First wait before retrying after the provider refused calls; doubles each time.",
    "agent.worker_pools.provider_limit.max_cooldown_seconds": "Longest wait between retries.",
    "agent.worker_pools.provider_limit.max_retries": "Retries before the task fails as provider-limited.",
    "agent.worker_pools.provider_limit.review_events": "Provider-limited tasks of one worker type within the window that give TeamArchitect a review.",
    "agent.worker_pools.provider_limit.review_window_minutes": "Window for counting provider-limited tasks.",
    "agent.worker_pools.progress_notices": "Counts trusted code sends the person while delegated tasks run.",
    "agent.worker_pools.progress_notices.min_tasks": "Requests with fewer delegated tasks get no progress notes.",
    "agent.worker_pools.progress_notices.interval_minutes": "At most one progress note per interval, and only when the counts changed.",
    "agent.schedule_maintenance.internal_improvement_review_interval_seconds": "InternalImprovement review cadence in seconds; each worker startup ensures one shared team schedule, the stale-schedule check runs before each review, and non-positive disables installation.",
    "agent.max_queued_top_level_tasks": "Maximum admitted top-level tasks tracked at once.",
    "agent.task_queue_warning_threshold": "Warn when pending top-level task count reaches this threshold.",
    "agent.subtask_timeout_seconds": "Local timeout for delegated subtasks and unanswered TaskQuery waits.",
    "agent.subtask_watchdog_interval_seconds": "Watchdog scan interval for overdue subtasks and TaskQuery waits.",
    "agent.subtask_status_probe_timeout_seconds": "Per-probe timeout for delegated-subtask status checks.",
    "agent.restart_executing_task_grace_seconds": "Restart grace for interrupted executing snapshots before failing fast.",
    "agent.same_role_runtime_takeover_enabled": "Same-role replacement agents may claim stale runtime snapshots.",
    "agent.request_conflict_checks": (
        "Refuse a request's change to a record another of the person's requests changed while it ran, and any "
        "change from a stopped or ended request; false keeps only \"changed since you read it\" checks. Agents "
        "cannot change it."
    ),
    "agent.max_tool_calls_per_task": "Cumulative tool calls across one ordinary local task-processing invocation, not per model turn. Accepted Objective work profiles use their own session total. Set null or <=0 to disable.",
    "agent.max_tool_result_tokens_per_call": "Per-tool-result token safety cap. Oversized results are offloaded to artifacts.",
    "agent.runtime_budget_policy": "Policy for recoverable runtime budget/overload pauses.",
    "agent.runtime_budget_policy.mode": '"hard_stop" fails visibly; "user_guided" pauses and asks the requester.',
    "agent.runtime_budget_policy.partial_tool_call_budget_multiplier": "Temporary tool-call budget multiplier when requester asks for partial results.",
    "agent.runtime_budget_policy.max_user_guided_pauses_per_task": "Maximum user-guided pauses before falling back to hard-stop behavior.",
    "agent.runtime_budget_policy.triggers": "Recoverable conditions that may pause instead of immediately failing.",
    "agent.runtime_budget_policy.triggers.tool_call_budget_exceeded": "Tool-call budget exceeded.",
    "agent.runtime_budget_policy.triggers.context_budget_exceeded": "Prompt/context budget exceeded.",
    "agent.runtime_budget_policy.triggers.delegated_subtask_timeout": "Delegated subtask timeout.",
    "agent.runtime_budget_policy.triggers.specialist_runtime_budget_pause": "Specialist agent runtime budget pause.",
    "agent.runtime_budget_policy.triggers.provider_overloaded": "Provider overload / 429 when failover cannot handle it.",
    "agent.turn_action_outbox": "Durable ordered action outbox for StructuredAgentTurnDecision side effects.",
    "agent.turn_action_outbox.max_auto_attempts": "Automatic retries for infrastructure-safe action failures.",
    "agent.turn_action_outbox.initial_retry_delay_seconds": "Initial automatic retry delay in seconds.",
    "agent.turn_action_outbox.max_retry_delay_seconds": "Maximum automatic retry delay in seconds.",
    "agent.turn_action_outbox.retry_jitter_ratio": "Jitter ratio applied to retry delays.",
    "agent.turn_action_outbox.max_inline_recovery_attempts_per_batch": "Inline model-recovery attempts before isolated recovery.",
    "agent.turn_action_outbox.max_validation_repair_attempts": "Reserved bounded validation-repair attempts for structured action output.",
    "agent.turn_action_outbox.dispatching_stale_after_seconds": "Restart normalization threshold for stale dispatching records.",
    "agent.runtime_coordinator": "Coordinates active action batches and defers conflicting inbound events.",
    "agent.runtime_coordinator.enabled": "Master switch for task-tree leases/mailbox deferral.",
    "agent.runtime_coordinator.mode": '"enforced" defers conflicts; "observe" logs diagnostics; "disabled" handles events directly.',
    "agent.runtime_coordinator.batch_lease_stale_after_seconds": "Stale lease threshold in seconds.",
    "agent.runtime_coordinator.max_deferred_events_per_task": "Maximum deferred mailbox events per task tree.",
    "agent.runtime_coordinator.max_mailbox_drain_events_per_pass": "Maximum mailbox events replayed in one drain pass.",
    "agent.runtime_coordinator.mailbox_event_retention_seconds": "Retain processed/stale/rejected mailbox events for this many seconds.",
    "agent.runtime_coordinator.check_interrupts_between_actions": "Check stop/cancel controls between ordered actions.",
    "agent.runtime_coordinator.defer_scheduled_events_during_active_batch": "Defer scheduled events while an action batch owns the task tree.",
    "agent.runtime_coordinator.defer_task_responses_during_active_batch": "Defer task responses while an action batch owns the task tree.",
    "agent.runtime_coordinator.defer_recovery_events_during_active_batch": "Defer recovery events while an action batch owns the task tree.",
    "agent.inner_loop_context": "Active Strands transcript management before model calls.",
    "agent.inner_loop_context.enabled": "Enable filtering/offloading of historical tool-result transcript.",
    "agent.inner_loop_context.max_total_tokens": "Main active-transcript token budget.",
    "agent.inner_loop_context.warning_ratio": "Emit a model-visible warning at this fraction of max_total_tokens.",
    "agent.inner_loop_context.aggressive_ratio": "Start aggressive artifact-only reduction at this fraction.",
    "agent.inner_loop_context.preserve_recent_messages": "Preserve recent messages from aggressive reduction where possible.",
    "agent.inner_loop_context.preserve_head_tokens": "Head excerpt tokens retained for filtered noisy results.",
    "agent.inner_loop_context.preserve_tail_tokens": "Tail excerpt tokens retained for filtered noisy results.",
    "agent.inner_loop_context.projected_token_pressure": "Proactive old-tool-result reduction based on projected input tokens.",
    "agent.inner_loop_context.projected_token_pressure.enabled": "Enable proactive old-tool-result reduction based on projected tokens.",
    "agent.inner_loop_context.projected_token_pressure.threshold_ratio": "Trigger when projected input reaches this context-window ratio.",
    "agent.inner_loop_context.projected_token_pressure.target_ratio": "Target projected context-window ratio after reduction.",
    "agent.inner_loop_context.reactive_overflow": "Bound reactive transcript reduction and provider reinvocation after a context rejection.",
    "agent.inner_loop_context.reactive_overflow.max_total_recovery_attempts": "Shared per-request ceiling for inner reductions and outer provider reinvocations.",
    "agent.inner_loop_context.reactive_overflow.min_reduction_tokens": "Minimum measured message-token reduction required before retrying the provider.",
    "agent.inner_loop_context.filters": "Built-in filters for noisy historical tool output.",
    "agent.inner_loop_context.filters.enabled": "Enable this historical tool-output filter.",
    "agent.inner_loop_context.filters.trigger_tokens": "Token threshold that activates this historical tool-output filter.",
    "agent.context_engine": "Outer-loop prompt assembly, durable context, and context-budget enforcement.",
    "agent.context_engine.type": 'Context engine implementation. Only "default" is currently supported.',
    "agent.context_engine.max_context_ratio": "Soft prompt-input budget ratio after reserving output tokens.",
    "agent.context_engine.reserved_output_tokens": "Output headroom subtracted from llm.context_window_tokens.",
    "agent.context_engine.hard_limit_behavior": 'Overflow behavior. "recover_then_error" tries bounded recovery first.',
    "agent.context_engine.overflow_recovery": "Bounded recovery when prompt assembly or provider call exceeds context.",
    "agent.context_engine.overflow_recovery.enabled": "Enable context-epoch/degraded-section recovery.",
    "agent.context_engine.overflow_recovery.max_preflight_attempts": "Maximum prompt preflight recovery attempts.",
    "agent.context_engine.overflow_recovery.max_provider_retry_attempts": "Maximum provider-overflow retry attempts after provider rejects context.",
    "agent.context_engine.overflow_recovery.degrade_sections": "Lower-priority sections that may be omitted during recovery only.",
    "agent.context_engine.context_epochs": "Durable conversation context-boundary summaries.",
    "agent.context_engine.context_epochs.enabled": "Enable context epoch records.",
    "agent.context_engine.context_epochs.auto_create": "Create epochs automatically when needed.",
    "agent.context_engine.context_epochs.summary_max_tokens": "Maximum epoch summary token budget.",
    "agent.context_engine.context_epochs.include_source_accounting": "Store/render source accounting with epoch summaries.",
    "agent.context_engine.strands_state_snapshots": "Optional future Strands-native snapshots. These are not canonical durable context.",
    "agent.context_engine.strands_state_snapshots.enabled": '"auto" feature-detects SDK snapshot support.',
    "agent.context_engine.strands_state_snapshots.persist": "Persist SDK snapshots when supported. false keeps this diagnostic-only.",
    "agent.context_engine.plugins": "Optional context lifecycle plugins.",
    "agent.context_engine.thread_window": "Conversation-thread rendering controls.",
    "agent.context_engine.thread_window.recent_full_turns": "Recent conversation turns kept in full when budget permits.",
    "agent.context_engine.thread_window.per_turn_summary_max_tokens": "Summary budget for older turns.",
    "agent.context_engine.thread_window.max_turns_to_load": "Maximum turns loaded from durable conversation storage.",
    "agent.context_engine.thread_window.omitted_notice_enabled": "Include notice when older turns are omitted.",
    "agent.context_engine.thread_window.include_turn_ids": "Include durable turn ids in rendered context.",
    "agent.context_engine.thread_window.retrieval_tool_hint": "Include hint for get_conversation_context_slice retrieval.",
    "agent.context_engine.task_runtime_timing": "Compact lifecycle timing added to model-visible context.",
    "agent.context_engine.task_runtime_timing.enabled": "Enable timing section.",
    "agent.context_engine.task_runtime_timing.include_message_created_at": "Include original message-created timestamp.",
    "agent.context_engine.task_runtime_timing.include_handled_at": "Include local handled-at timestamp.",
    "agent.context_engine.task_runtime_timing.include_processing_started_at": "Include local processing-start timestamp.",
    "agent.context_engine.task_runtime_timing.include_processing_finished_at": "Include processing-finished timestamp when available.",
    "agent.debug": "Optional context and runtime diagnostics.",
    "agent.debug.terminal_reasoning": "Local terminal display for provider-exposed reasoning deltas.",
    "agent.debug.terminal_reasoning.enabled": "Print provider-exposed reasoning deltas to stderr when callbacks include them.",
    "agent.debug.context_dump": "Model-visible context/runtime trace dump settings.",
    "agent.debug.context_dump.enabled": "Enable model-visible context/runtime trace dumps.",
    "agent.debug.context_dump.mode": "Dump mode: always, on_error, or on_budget_warning.",
    "agent.debug.context_dump.include_system_prompt": "Include the full system prompt file/rendering.",
    "agent.debug.context_dump.include_full_prompt": "Include full assembled prompt.",
    "agent.debug.context_dump.include_context_sections": "Include context sections as structured JSON.",
    "agent.debug.context_dump.include_source_lengths": "Include per-source size accounting.",
    "agent.debug.context_dump.include_incoming_envelope_json": "Include full incoming envelope JSON. Usually keep false for sensitivity/noise.",
    "agent.debug.context_dump.include_strands_messages": "Include Strands message snapshots.",
    "agent.debug.context_dump.include_tool_results": "Include tool-result diagnostics.",
    "agent.debug.context_dump.include_result": "Include parsed/returned result data.",
    "agent.debug.context_dump.include_reasoning_artifacts": "Include observable reasoning artifacts, not hidden chain-of-thought.",
    "agent.debug.context_dump.capture_model_calls": "Opt in to per-provider-call accounting through feature-detected Strands middleware.",
    "agent.debug.context_dump.include_model_call_inputs": "Include redacted messages, system prompt, and tool-choice snapshots in model-call details.",
    "agent.debug.context_dump.include_tool_specs": "Include redacted full tool schemas in model-call details; compact schema-size accounting is always captured.",
    "agent.debug.context_dump.redact_secrets": "Redact secret-looking values in dumps.",
    "agent.debug.context_dump.retention_days": "Best-effort dump retention in days.",
    "agent.debug.context_dump.web_chat_controls_enabled": "Show authenticated Web Chat controls for live context dump toggles.",
    "agent.debug.reply_routing_trace": "Reply routing trace settings for temporary diagnostics.",
    "agent.debug.reply_routing_trace.enabled": "Trace reply routing boundaries for temporary diagnostics.",
    "agent.structured_output_failure_threshold": "Optional threshold for stopping repeated structured-output validation failures.",
    "agent.deferred_message_warning_threshold": "Warn when deferred inbound delivery backlog reaches this count.",
    "agent.max_deferred_messages": "Maximum durable deferred admission count; excess work remains with NATS for redelivery.",
    "agent.conversation_compactor": "Conversation compaction policy for durable conversation history.",
    "agent.conversation_compactor.type": "Compactor implementation: heuristic_sliding_window, llm_summarizing, or noop.",
    "agent.conversation_compactor.options": "Compactor tuning options.",
    "agent.conversation_compactor.options.trigger_ratio": "Trigger compaction when the configured ratio is reached.",
    "agent.conversation_compactor.options.target_ratio": "Target ratio after compaction.",
    "agent.conversation_compactor.options.min_recent_turns": "Minimum recent turns to preserve.",
    "agent.conversation_compactor.options.max_recent_turns": "Maximum recent turns to preserve.",
    "agent.conversation_summary_generator_type": 'Durable per-turn summary generator. "heuristic" is deterministic; "llm_summarizing" uses model summaries with fallback.',
    "agent.tool_allowlist": "Optional exact tool-name allowlist. null means no allowlist; [] means no tools. A name that is no tool function is refused.",
    "agent.tool_blocklist": "Optional exact tool-name blocklist applied after allowlist filtering. A name that is no tool function is refused.",
    "agent.external_mcp_servers": "Static named MCP servers for every agent type; with authorization on an agent uses one only with access to its mcp:<name> tool. Empty global values configure none. Agents start without a server that does not answer and load it when it does (mcp.load_retry_window_minutes).",
    "agent.agent_definition_paths": "Additional YAML agent-definition files or directories. Relative paths resolve under control/.",
    "agent.prompt_skill_description_exclusions": "Skill names hidden from AgentSkills menu/XML prompt content.",
    "agent.prompt_skill_description_limit": "Maximum deduplicated skill descriptions shown in the AgentSkills prompt menu; all discovered skills remain activatable.",
    "workspace": "Workspace-owned directories and operator-editable runtime assets.",
    "workspace.objective_execution_enabled": "Make bounded Objective execution profiles available for explicit per-Objective activation.",
    "workspace.objective_execution_paths": "Ordered built-in policy URIs or bounded Objective execution policy paths; relative paths resolve under control/.",
    "workspace.skill_installation.enabled": "Allow TeamArchitect to stage reviewed skills from public GitHub repositories.",
    "workspace.skill_installation.scanner_executable": "Operator-owned SkillSpector executable name or absolute path.",
    "workspace.skill_installation.scanner_timeout_seconds": "Maximum static scan runtime.",
    "workspace.skill_installation.safe_max_score": "Maximum SkillSpector score accepted as SAFE; cannot exceed 20.",
    "workspace.skill_installation.plan_ttl_seconds": "Lifetime of an immutable scanned installation plan.",
    "workspace.skill_installation.max_repository_bytes": "Maximum quarantined Git repository size.",
    "workspace.external_skill_repositories": "Public GitHub repositories containing skills directories at any depth that can be refreshed before agent startup.",
    "workspace.refresh_external_skills_on_start": "Refresh configured external skill repositories before agents start.",
    "assistant": "Assistant-facing profile defaults.",
    "assistant.display_name": "Assistant display name shown in user-facing surfaces.",
    "assistant.manager_transition_notices_enabled": "Emit proactive manager outage/recovery messages into the configured delivery channel.",
    "assistant.unavailable_message_template": "Manager-unavailable message template. Supports {assistant_name}.",
    "assistant.available_message_template": "Manager-available message template. Supports {assistant_name}.",
    "user": "Human user profile defaults.",
    "user.display_name": "Human display name used for bootstrap context.",
    "user.canonical_user_id": "Stable user id used for canonical user memory and identity records.",
    "user.timezone": "IANA timezone used to interpret relative dates when requests omit one.",
    "user.default_session_key": "Default gateway session key for local or unscoped interactions.",
    "user.role": "Optional user role/context label rendered into bootstrap guidance.",
    "delivery": "Notification delivery, quiet-hour, and digest defaults.",
    "delivery.quiet_hours": "Quiet-hours policy for proactive delivery.",
    "delivery.quiet_hours.enabled": "Defer non-urgent proactive delivery during quiet hours.",
    "delivery.quiet_hours.start_hour_local": "Local quiet-hours start, 0-23.",
    "delivery.quiet_hours.end_hour_local": "Local quiet-hours end, 0-23.",
    "delivery.quiet_hours.allow_urgent_delivery": "Urgent messages can bypass quiet-hours deferral.",
    "delivery.digest_delivery_hour_local": "Local hour to release deferred-message digests.",
    "delivery.default_target": "Preferred user communication target.",
    "delivery.default_target.channel_name": "Preferred channel, e.g. cli, telegram, discord, or whatsapp.",
    "delivery.default_target.chat_id": "Preferred destination id as a string. Quote negative Telegram ids.",
    "delivery.default_target.thread_id": "Optional Discord thread id or Telegram forum topic id as a string.",
    "delivery.notification_routing": "Routing policy for agent UserNotification messages.",
    "delivery.notification_routing.mode": "How notify_user target='auto' routes: source_thread_or_default or default_target.",
    "delivery.summarize_deferred": "Summarize deferred messages before digest delivery.",
    "delivery.urgent_keywords": "Keywords that bypass quiet hours.",
    "heartbeat": "Heartbeat schedule and source settings.",
    "heartbeat.enabled": "Enable recurring heartbeat prompts.",
    "heartbeat.interval_seconds": "Heartbeat interval in seconds.",
    "heartbeat.session_key": "Gateway session key used for heartbeat output.",
    "heartbeat.source_id": "Persistent heartbeat source id.",
    "heartbeat.schedule_id": "Persistent heartbeat launcher schedule id.",
    "heartbeat.active_hours_enabled": "Restrict heartbeat delivery to active hours.",
    "heartbeat.active_start_hour_local": "Local active-hours start, 0-23.",
    "heartbeat.active_end_hour_local": "Local active-hours end, 0-23.",
    "llm": "Default model, provider, and inference behavior.",
    "llm.provider": "Main chat/reasoning model provider.",
    "llm.model_id": "Main chat/reasoning model id.",
    "llm.temperature": (
        "Sampling temperature for providers that support it. OpenAI GPT-5/o-series and "
        "OpenAI Codex routes omit this unsupported parameter."
    ),
    "llm.reasoning_effort": "Optional reasoning-effort hint translated to the selected provider transport.",
    "llm.reasoning_summary": (
        "Optional provider-generated reasoning summary for OpenAI Responses requests: "
        "auto, concise, detailed, or null."
    ),
    "llm.litellm_api_mode": (
        "Request API used by the litellm provider path: chat_completions for broad "
        "compatibility or responses for a Responses-capable endpoint."
    ),
    "llm.context_window_tokens": "Explicit context-window size used for prompt budgeting and compaction.",
    "llm.max_output_tokens": "Optional provider-supported cap on generated output tokens.",
    "llm.stream": (
        "Optional LiteLLM streaming override for agent model calls; Responses mode "
        "requires streaming."
    ),
    "llm.request_timeout_seconds": (
        "HTTP timeout for model requests: longest wait to connect or for the next bytes. "
        "Also bounds a model-switch probe."
    ),
    "llm.stream_idle_timeout_seconds": (
        "Longest wait for the next model output; keep-alive pings do not count. "
        "A stalled call then fails over or ends the task with a timeout message."
    ),
    "llm.model_call_timeout_seconds": "Longest time one model call may take in total.",
    "llm.supports_image_input": "Whether the selected provider/model accepts image inputs. null means unknown.",
    "llm.supports_audio_input": "Whether the selected provider/model accepts audio inputs. null means unknown.",
    "llm.supports_file_input": (
        "Whether the selected provider/model accepts native file/document inputs. null means unknown."
    ),
    "llm.model_capabilities": "Provider/model-specific capability facts keyed by provider and model id.",
    "llm.reasoning_trace_retention_messages": "Later-message retention window for full provider reasoning traces after tool-call turns.",
    "llm.litellm_api_base": (
        "Chat Completions-compatible base URL used by the litellm provider path. "
        "This may be OpenRouter, another compatible service, or a LiteLLM proxy."
    ),
    "llm.openai_codex": "OpenAI Codex OAuth provider settings. Used only when llm.provider is openai-codex.",
    "llm.openai_codex.base_url": "OpenAI Codex backend base URL.",
    "llm.openai_codex.auth_store_path": "Universal Agents-owned Codex OAuth token store path.",
    "llm.openai_codex.shared_codex_home": "Optional Codex CLI home used for read-only file-auth discovery.",
    "llm.openai_codex.allow_shared_codex_auth_read": "Allow read-only access to file-based Codex CLI auth when no owned auth store is available.",
    "llm.openai_codex.refresh_skew_seconds": "Refresh owned Codex OAuth tokens this many seconds before JWT expiry.",
    "llm.openai_codex.refresh_timeout_seconds": "HTTP timeout for Codex OAuth token refresh requests.",
    "llm.openai_codex.model_discovery": "Codex OAuth model discovery and cache settings.",
    "llm.openai_codex.model_discovery.enabled": "Enable live Codex model discovery for status/onboarding suggestions.",
    "llm.openai_codex.model_discovery.cache_path": "Codex model discovery cache path.",
    "llm.openai_codex.model_discovery.cache_ttl_seconds": "How long cached Codex model discovery results are considered fresh.",
    "llm.openai_codex.model_discovery.client_version": "Client version query parameter sent to the Codex models endpoint.",
    "llm.xai_oauth": "xAI Grok OAuth provider settings. Used only when llm.provider is xai-oauth.",
    "llm.xai_oauth.base_url": "xAI Responses API base URL; HTTPS x.ai origins only.",
    "llm.xai_oauth.auth_store_path": "Universal Agents-owned xAI OAuth token store path. Tokens are never stored in YAML.",
    "llm.xai_oauth.refresh_skew_seconds": "Maximum proactive refresh window for xAI OAuth access tokens.",
    "llm.xai_oauth.refresh_timeout_seconds": "HTTP timeout for xAI OAuth discovery, device login, and token refresh requests.",
    "llm.failover": "Runtime-owned model-provider failover policy.",
    "llm.failover.enabled": "Master switch for cross-provider failover. false keeps same-provider behavior.",
    "llm.failover.same_provider_retry": "Same-provider throttling retry configuration used before cross-provider failover.",
    "llm.failover.same_provider_retry.max_attempts": "Total same-provider model attempts before cross-provider failover.",
    "llm.failover.same_provider_retry.initial_delay_seconds": "Delay before the first same-provider retry, in seconds.",
    "llm.failover.same_provider_retry.max_delay_seconds": "Maximum retry delay, in seconds.",
    "llm.failover.switch_for": "Minimum lease duration for a selected fallback.",
    "llm.failover.switch_for.min_turns": "Minimum successful model turns to keep using the fallback.",
    "llm.failover.switch_for.min_seconds": "Minimum seconds to keep using the fallback.",
    "llm.failover.demotion": "Historical failure demotion. Repeated failures push a provider behind healthy alternatives.",
    "llm.failover.demotion.failure_threshold": "Failure count that triggers demotion.",
    "llm.failover.demotion.failure_window_seconds": "Rolling failure window in seconds.",
    "llm.failover.demotion.demotion_seconds": "Demotion duration in seconds.",
    "llm.failover.history_retention_seconds": "Retain provider success/failure history for this many seconds.",
    "llm.failover.notify_user": "Emit user-visible activity and task-history entries when switching.",
    "llm.failover.fallbacks": "Ordered fallback provider/model candidates.",
    "llm.failover.fallbacks.model_id": "Fallback model id.",
    "embedding": "Embedding provider and model settings.",
    "embedding.provider": "Embedding backend: local or openai_compatible.",
    "embedding.model_id": "Embedding model id. Required for openai_compatible.",
    "embedding.api_base": "OpenAI-compatible embedding base URL.",
    "embedding.dimensions": "Optional explicit embedding dimensionality.",
    "embedding.timeout_seconds": "Embedding request timeout in seconds.",
    "embedding.organization": "Optional OpenAI-compatible organization header.",
    "embedding.extra_headers": "Optional non-secret static headers for embedding requests.",
    "voice": "Voice input/output behavior and model defaults.",
    "voice.enabled": "Master voice switch.",
    "voice.input_enabled": "Enable inbound speech-to-text.",
    "voice.output_enabled": "Enable outbound text-to-speech.",
    "voice.response_mode": "Reply mode: voice_only, voice_and_text, or text_only.",
    "voice.provider": 'Voice provider. "grok" is implemented; "litellm_proxy" is reserved.',
    "voice.api_base": "Voice API base URL.",
    "voice.stt_language": "Speech-to-text language hint.",
    "voice.stt_format_text": "Ask STT to format text.",
    "voice.tts_voice": "Text-to-speech voice name.",
    "voice.tts_language": "Text-to-speech language hint.",
    "voice.tts_output_codec": "Text-to-speech output codec.",
    "voice.tts_sample_rate": "Text-to-speech sample rate.",
    "voice.tts_bit_rate": "Text-to-speech bit rate.",
    "voice.connect_timeout_seconds": "Voice HTTP connect timeout in seconds.",
    "voice.request_timeout_seconds": "Voice HTTP request timeout in seconds.",
    "tool_policy": "Tool settings that are not access (access is given with grants).",
    "tool_policy.scratch_retention_days": "Days a file stays in a scratch/ folder (people's and agents/, including copies of files sent to people) before the internal-improvement maintenance run deletes it; 0 keeps them. Administrators can also change it in Settings > Team; it applies after a restart.",
    "tool_policy.output_retention_days": "Days saved tool outputs are kept after they were written before the internal-improvement maintenance run deletes them: oversized tool results (state/tool_results), long tool output offloaded from conversations (dbs/conversation_artifacts and their records), research results (research/ in people's folders and agents/) and execution run folders (agents/execution). Conversations, their summaries, task links and completed tasks stay. 0 keeps them; at most 3650. Administrators can also change it in Settings > Team; it applies after a restart.",
    "authorization": "Access checks: agents and people need grants for the tools they use.",
    "authorization.enabled": "Check every agent tool call and broker request against grants; false runs every tool without requests while the sandbox, broker and protected folders still apply.",
    "authorization.cedar_binary_path": "Optional path or command name of the ua-cedar-authorizer access evaluator; null finds it.",
    "authorization.starter_profile": "The first user's default for the team lead's standing access to their work, until they choose their own: trusted, careful, or manual.",
    "authorization.presentation": "How much detail access refusals and review messages show.",
    "authorization.presentation.mode": "friendly uses plain language; technical also shows exact tool ids, levels, identifiers and safe failure details.",
    "access_workflows": "Access workflows: the plans agents publish and people follow on the workflow page.",
    "access_workflows.max_steps": "Maximum steps in one access workflow.",
    "access_workflows.diagnostic_event_limit": "Maximum redacted events per workflow diagnostic download.",
    "agent_types": "Per-agent-type runtime and LLM overrides.",
    "mcp_profiles": "MCP profile definitions available to agent tools.",
    "ui": "User-interface preferences shared by onboarding and runtime web surfaces.",
    "ui.onboarding": "Onboarding UI preferences retained for the post-reboot interface.",
    "ui.onboarding.appearance": "Shared color theme and background preferences for web interfaces.",
    "ui.onboarding.appearance.color_mode": "Default interface theme: dark or light.",
    "ui.onboarding.appearance.light_colors": "Four background light colors for onboard-web.",
    "ui.onboarding.appearance.light_diffusion": "Light diffusion percentage, 0 concentrated through 100 diffuse.",
    "ui.onboarding.appearance.polygon_opacity_low": "Low end of moving polygon opacity percentage.",
    "ui.onboarding.appearance.polygon_opacity_high": "High end of moving polygon opacity percentage.",
    "ui.onboarding.appearance.polygon_line_color": "Polygon edge color.",
    "ui.onboarding.appearance.polygon_line_opacity": "Polygon edge opacity percentage.",
    "ui.onboarding.appearance.polygon_seed": "Stable seed for the diffuse light polygon mesh.",
    "ui.onboarding.appearance.dynamic_background": "Animate the onboarding background.",
    "ui.onboarding.appearance.interface_animation": "Animate interface elements in Web Chat and onboarding (panels, messages, the activity mesh); reduced-motion systems never animate.",
    "ui.onboarding.appearance.dark": "Dark metallic background: polished metal plates in a dark studio.",
    "ui.onboarding.appearance.dark.detail": "full draws moving metal with the graphics card; basic draws a still version without it.",
    "ui.onboarding.appearance.dark.surface_color": "Dark surroundings that unlit plates reflect.",
    "ui.onboarding.appearance.dark.metal_color": "Base metal color of the plates.",
    "ui.onboarding.appearance.dark.facet_size": "Average plate size in pixels, from 140 through 520.",
    "ui.onboarding.appearance.dark.facet_depth": "How far the plates tilt, from 0 (flat) through 100.",
    "ui.onboarding.appearance.dark.chrome_faces": "Share of plates turned toward a light, from 0 through 20 percent.",
    "ui.onboarding.appearance.dark.shine": "Light strength, from 0 through 100.",
    "ui.onboarding.appearance.dark.edge_highlights": "Width of the bright plate edges, from 0 through 100.",
    "ui.onboarding.appearance.dark.surface_grain": "Brushed metal grain, from 0 through 100.",
    "ui.onboarding.appearance.dark.light_x": "Light position left (-) or right (+) of the screen center, from -300 through 300; 100 is the screen edge.",
    "ui.onboarding.appearance.dark.light_y": "Light position above (-) or below (+) the screen center, from -300 through 300; 100 is the screen edge.",
    "ui.onboarding.appearance.dark.light_softness": "Light size and edge softness, from 0 (small, sharp) through 100 (large, even).",
    "ui.onboarding.appearance.dark.light_motion": "Move the light and tilt the plates slowly.",
    "ui.onboarding.appearance.dark.light_wander": "Let the light roam around its place so it lights different parts of the screen over time (needs light_motion).",
    "ui.onboarding.appearance.dark.light_wander_distance": "How far the wandering light roams from its place, from 0 through 150; 100 is the distance from the screen center to its edge.",
    "ui.onboarding.appearance.dark.light_speed": "Reflection speed percentage, from 10 through 400.",
    "ui.onboarding.appearance.dark.mesh_motion": "Let the plates drift and reconnect.",
    "ui.onboarding.appearance.dark.mesh_speed": "Mesh speed percentage, from 10 through 400.",
    "ui.onboarding.appearance.dark.seed": "Stable seed for the plate layout.",
    "ui.onboarding.appearance.dark.accents": "Gold and bronze tints and the share of plates (0 through 50 percent) each covers.",
    "security_layer": "Secure tool broker and credential-store settings.",
    "security_layer.mode": "Security mode: broker, or openshell to also sandbox agent commands. The secure tool broker always runs.",
    "security_layer.broker_internal_base_url": "Agent/gateway-facing broker API URL.",
    "security_layer.broker_public_port": "Browser-facing broker port combined with network.public_origin.",
    "security_layer.broker_bind_host": "Broker listener host.",
    "security_layer.broker_bind_port": "Broker listener port.",
    "security_layer.broker_credentials_dir": "Broker credential-state directory. Relative paths resolve under instance_root.",
    "security_layer.tool_setup_recheck_days": "Days after which the broker re-checks a ready tool setup nobody used or checked since, and tells the person in the Inbox when it needs setting up again; 0 turns it off.",
    "security_layer.code_execution": "Sandbox for agent-run shell and python_env commands outside OpenShell mode.",
    "security_layer.code_execution.sandbox": "required confines each command to the workspace, a private temp HOME, and read-only OS files, refusing commands when the sandbox cannot apply; off runs agent code unconfined.",
    "security_layer.code_execution.extra_read_paths": "Extra absolute paths sandboxed code may read. Never list the instance root.",
    "security_layer.code_execution.extra_write_paths": "Extra absolute paths sandboxed code may read and write.",
    "security_layer.credential_state_backend": "Broker auth payload persistence settings.",
    "security_layer.credential_state_backend.mode": '"files" or "credential_source".',
    "security_layer.credential_state_backend.source": "Credential source when mode is credential_source, e.g. sealbox, vault, or bitwarden.",
    "security_layer.credential_state_backend.namespace": "Credential-source namespace/path prefix.",
    "security_layer.credential_state_backend.fallback_to_files": "Use broker files for absent records or other-source failures. Sealbox errors always stop the operation.",
    "security_layer.credential_catalog": "Credential catalog settings for approved credential bindings.",
    "security_layer.credential_catalog.enabled": "Enable secure credential collection, signup fallback, and OpenShell binding flows.",
    "security_layer.credential_catalog.database_path": "Owner-scoped SQLite credential catalog. Relative paths resolve under instance_root.",
    "security_layer.credential_catalog.template_overrides_path": "Credential template override file. Relative paths resolve under instance_root.",
    "security_layer.credential_catalog.default_write_source": "Default store for new secrets. Unsupported write targets fail closed.",
    "security_layer.credential_catalog.openshell_binding_ttl_seconds": "Per-use OpenShell credential binding lease lifetime.",
    "security_layer.credential_catalog.collection_session_ttl_seconds": "Secure broker form session lifetime.",
    "security_layer.credential_catalog.secret_cache_ttl_seconds": "In-memory broker cache TTL for resolved catalog secrets; 0 disables.",
    "security_layer.bitwarden_server_url": "Optional Bitwarden/Vaultwarden server URL configured before broker-side bw commands.",
    "security_layer.sealbox": "Independent encrypted credential service used by the broker; relative client paths resolve under broker_credentials_dir.",
    "security_layer.sealbox.enabled": "Configure Sealbox as a broker credential source.",
    "security_layer.sealbox.manage_service": "Start sealbox-server during headless launch.",
    "security_layer.sealbox.auto_initialize": "Create the managed service root token when missing; user tenants are provisioned lazily.",
    "security_layer.sealbox.bind_host": "Managed sealbox-server listener host.",
    "security_layer.sealbox.bind_port": "Managed sealbox-server listener port.",
    "security_layer.sealbox.store_path": "Sealbox SQLite store. Relative paths resolve under broker_credentials_dir.",
    "security_layer.sealbox.service_url": "External Sealbox service URL; null derives from the managed bind host and port.",
    "security_layer.sealbox.root_token_file": "Root administration token used only for tenant lifecycle operations.",
    "security_layer.sealbox.tenant_material_dir": "Broker-only directory containing opaque per-owner tenant tokens and key pairs.",
    "security_layer.sealbox.api_version": "Sealbox client API version. Multi-user operation requires v2.",
    "security_layer.sealbox.ready_timeout_seconds": "Seconds to wait for managed sealbox-server readiness.",
    "security_layer.openshell": "OpenShell sandbox/gateway settings used when mode is openshell.",
    "security_layer.openshell.sandbox_name": "Name prefix of the per-person sandboxes (<prefix>-p-<person>, <prefix>-shared). null defaults to team-<instance>.",
    "security_layer.openshell.sandbox_image": "Optional sandbox image/community name/path.",
    "security_layer.openshell.policy_file": "OpenShell sandbox policy file; it decides the network and what else OpenShell code may reach.",
    "security_layer.openshell.workspace_mount": "Path inside each sandbox where the person's folder and team/ and agents/ are copied.",
    "security_layer.openshell.manage_gateway": "Start/manage openshell-gateway in headless mode.",
    "security_layer.openshell.gateway_name": "OpenShell gateway name. null defaults to team-<instance>.",
    "security_layer.openshell.gateway_bind_host": "OpenShell gateway listener host.",
    "security_layer.openshell.gateway_bind_port": "OpenShell gateway listener port.",
    "security_layer.openshell.gateway_endpoint": "Gateway endpoint used by clients. null derives from bind host/port.",
    "security_layer.openshell.gateway_driver": "OpenShell backend driver.",
    "security_layer.openshell.gateway_state_dir": "Gateway state directory. null uses <instance_root>/runtime/openshell/state/openshell.",
    "security_layer.openshell.gateway_db_url": "Gateway DB URL. null derives from gateway_state_dir.",
    "security_layer.openshell.gateway_binary": "Gateway binary path/name.",
    "security_layer.openshell.gateway_ready_timeout_seconds": "Seconds to wait for gateway process readiness.",
    "security_layer.openshell.gateway_extra_args": "Extra openshell-gateway argv entries.",
    "security_layer.openshell.create_if_missing": "Create a person's sandbox the first time their code runs.",
    "security_layer.openshell.delete_on_shutdown": "Delete this instance's sandboxes when the launcher stops.",
    "security_layer.openshell.upload_workspace": "Copy the person's folder and team/ and agents/ into their sandbox when it is created.",
    "security_layer.openshell.no_git_ignore": "Ignore .gitignore while copying folders into a sandbox.",
    "security_layer.openshell.provision_timeout_seconds": "Sandbox provisioning timeout in seconds.",
    "security_layer.openshell.readiness_timeout_seconds": "Sandbox readiness timeout in seconds.",
    "activity_collector": "Runtime activity stream and status collection settings.",
    "activity_collector.base_url": "Agent-side collector URL override; leave blank to derive from bind_host and bind_port.",
    "activity_collector.connect_timeout_seconds": "HTTP connect timeout in seconds.",
    "activity_collector.request_timeout_seconds": "HTTP request timeout in seconds.",
    "activity_collector.batch_max_events": "Maximum events sent per collector request.",
    "activity_collector.flush_interval_seconds": "Flush interval in seconds.",
    "activity_collector.max_buffered_events": "Maximum buffered events before dropping/backpressure.",
    "activity_collector.drop_when_unavailable": "Drop activity events when collector is unavailable instead of blocking work.",
    "activity_collector.bind_host": "Collector service listener host for run-activity-collector.",
    "activity_collector.bind_port": "Collector service listener port.",
    "activity_collector.user_url_path": "User dashboard URL path.",
    "activity_collector.admin_url_path": "Admin dashboard URL path.",
    "activity_collector.otel_trace_base_url": "Optional OTEL trace UI base URL for linking traces.",
    "activity_collector.ingest_token_env_var": "Env var containing the collector ingest token; keep the value in team.env or a secret file.",
    "activity_collector.storage": "Activity collector persistence store.",
    "activity_collector.storage.backend": "Activity collector backend: sqlite or postgres.",
    "activity_collector.storage.sqlite": "SQLite settings for activity collector events.",
    "activity_collector.storage.sqlite.path": "Activity collector SQLite DB. Relative paths resolve under instance_root.",
    "activity_collector.storage.postgres": "Postgres settings for activity events; connection strings stay in the environment.",
    "activity_collector.storage.postgres.schema_name": "Postgres schema for activity collector tables.",
    "interactive_apps": "Human-facing app integrations and their listener settings.",
    "interactive_apps.plugins": "Registry-keyed adapter settings and the exact allowlist for optional packages.",
    "interactive_apps.adapter_host": "Authenticated private process boundary for adapters selected with execution=adapter_host.",
    "interactive_apps.adapter_host.enabled": "Start the gateway-side adapter-host API and permit adapter_host plugin execution.",
    "interactive_apps.adapter_host.transport": "Private transport: unix for same-host deployments or mtls for container/remote deployments.",
    "interactive_apps.adapter_host.host_id": "Stable authenticated identity presented by the managed Python adapter-host process.",
    "interactive_apps.adapter_host.allowed_host_ids": "Exact workload identities allowed to register configured adapter instances.",
    "interactive_apps.adapter_host.token_env_var": "Env var containing the rotatable adapter-host bearer token; the token value never belongs in YAML.",
    "interactive_apps.adapter_host.unix_socket_path": "Permission-restricted Unix socket path. Relative paths resolve under instance_root.",
    "interactive_apps.adapter_host.unix_socket_mode": "Octal permission mode applied to the Unix socket after binding.",
    "interactive_apps.adapter_host.bind_host": "Listener host used only by the mTLS transport.",
    "interactive_apps.adapter_host.bind_port": "Listener port used only by the mTLS transport.",
    "interactive_apps.adapter_host.connect_host": "Gateway DNS name or address used by the adapter-host mTLS client.",
    "interactive_apps.adapter_host.tls": "Mutual-TLS certificate and trust paths for container or remote private transport.",
    "interactive_apps.adapter_host.tls.server_certificate_path": "Gateway server certificate path; relative paths resolve under instance_root.",
    "interactive_apps.adapter_host.tls.server_private_key_path": "Gateway server private-key path; keep file permissions restrictive.",
    "interactive_apps.adapter_host.tls.client_ca_path": "CA bundle used by the gateway to authenticate adapter-host client certificates.",
    "interactive_apps.adapter_host.tls.client_certificate_path": "Adapter-host client certificate path.",
    "interactive_apps.adapter_host.tls.client_private_key_path": "Adapter-host client private-key path; keep file permissions restrictive.",
    "interactive_apps.adapter_host.tls.server_ca_path": "CA bundle used by the adapter host to authenticate the gateway server.",
    "interactive_apps.adapter_host.tls.server_name": "Expected gateway TLS server name.",
    "interactive_apps.adapter_host.limits": "Gateway ceilings intersected with host-supported limits at registration.",
    "interactive_apps.adapter_host.limits.max_in_flight_events": "Maximum received but incomplete deliveries for one adapter stream.",
    "interactive_apps.adapter_host.limits.max_payload_bytes": "Maximum JSON payload size negotiated for one protocol message.",
    "interactive_apps.adapter_host.limits.max_attachment_bytes": "Maximum bytes accepted by one opaque attachment upload ticket.",
    "interactive_apps.adapter_host.limits.max_pending_events": "Bounded durable outbox capacity per adapter stream.",
    "interactive_apps.adapter_host.limits.heartbeat_interval_seconds": "Maximum interval between adapter-host lease heartbeats.",
    "interactive_apps.adapter_host.limits.acknowledgement_timeout_seconds": "Time allowed for delivery receipt/completion progress before health degrades.",
    "interactive_apps.adapter_host.limits.retention_seconds": "Retention for terminal inbox/outbox records used by reconnect validation.",
    "interactive_apps.adapter_host.lease_seconds": "Registration lease duration renewed by authenticated heartbeats.",
    "interactive_apps.adapter_host.upload_ticket_ttl_seconds": "Lifetime of a bounded one-use inbound attachment upload ticket.",
    "interactive_apps.adapter_host.upload_dir": "Gateway-owned attachment transfer directory. Relative paths resolve under instance_root.",
    "interactive_apps.adapter_host.client_state_path": "Adapter-host local idempotency database. Relative paths resolve under instance_root.",
    "interactive_apps.adapter_host.reconnect_initial_delay_seconds": "Initial reconnect delay for the adapter-host client.",
    "interactive_apps.adapter_host.reconnect_max_delay_seconds": "Maximum bounded reconnect delay for the adapter-host client.",
    "interactive_apps.plugins.builtin.internal_website.settings": "Internal website and Web Chat settings.",
    "interactive_apps.plugins.builtin.internal_website.settings.enabled": "Start shared docs/generated-site host with run-gateway unless --no-apps is used.",
    "interactive_apps.plugins.builtin.internal_website.settings.bind_host": "Listener host for the shared docs/generated-site service.",
    "interactive_apps.plugins.builtin.internal_website.settings.bind_port": "Shared docs/generated-site listener port.",
    "interactive_apps.plugins.builtin.internal_website.settings.public_port": "Browser-facing website port combined with network.public_origin.",
    "interactive_apps.plugins.builtin.internal_website.settings.docs_root": "User docs root. Relative paths resolve from the runtime working directory.",
    "interactive_apps.plugins.builtin.internal_website.settings.skills_dir": "Team skill-docs source directory; empty uses control/skills. Attested managed installations are shown separately. Relative paths resolve under control/.",
    "interactive_apps.plugins.builtin.internal_website.settings.web_chat_enabled": "Serve built-in Web Chat through the internal website host.",
    "interactive_apps.plugins.builtin.internal_website.settings.web_chat_token_env_var": "Env var containing the Web Chat token; keep the value in team.env or a secret file.",
    "interactive_apps.plugins.builtin.internal_website.settings.attachments": "Built-in Web Chat attachment settings.",
    "interactive_apps.plugins.builtin.internal_website.settings.attachments.enabled": "Allow uploads, pasted images, and attachment rendering in Web Chat.",
    "interactive_apps.plugins.builtin.internal_website.settings.attachments.max_file_bytes": "Maximum accepted Web Chat attachment size in bytes.",
    "interactive_apps.plugins.builtin.internal_website.settings.attachments.retention_days": "Days the gateway keeps a file people send in any chat app (state/uploads and its record) before deleting it; 0 keeps them. Administrators can also change it in Settings > Team.",
    "interactive_apps.plugins.builtin.internal_website.settings.attachments.allowed_kinds": "Attachment kinds accepted by Web Chat.",
    "interactive_apps.plugins.builtin.internal_website.settings.attachments.nats_object_bucket": "NATS Object Store bucket for team-lead-coordinated remote copies.",
    "interactive_apps.plugins.builtin.internal_website.settings.attachments.nats_object_ttl_seconds": "Retention seconds for NATS Object Store attachment copies.",
    "interactive_apps.plugins.builtin.internal_website.settings.attachments.nats_object_max_bytes": "Optional max bytes for the attachment Object Store bucket.",
    "interactive_apps.plugins.builtin.internal_website.settings.attachments.remote_copy_enabled": "Allow gateway-owned on-demand mirroring to NATS Object Store.",
    "interactive_apps.plugins.builtin.telegram.settings": "Telegram app settings.",
    "interactive_apps.plugins.builtin.discord.settings": "Discord app settings.",
    "interactive_apps.plugins.builtin.whatsapp.settings": "WhatsApp app settings.",
    "interactive_apps.plugins.builtin.signal.settings": "Signal app settings for signal-cli-rest-api.",
    "interactive_apps.plugins.builtin.photon.settings": "Photon Spectrum iMessage app settings.",
    "interactive_apps.plugins.builtin.telegram.settings.enabled": "Enable the Telegram interactive adapter.",
    "interactive_apps.plugins.builtin.telegram.settings.allowed_user_ids": "Telegram user ids allowed to contact the team.",
    "interactive_apps.plugins.builtin.telegram.settings.allowed_chat_ids": "Telegram chat ids allowed to contact the team.",
    "interactive_apps.plugins.builtin.telegram.settings.group_policy": "Telegram group handling policy: mention or open.",
    "interactive_apps.plugins.builtin.telegram.settings.manager_offline_policy": "Inbound behavior when the manager is offline.",
    "interactive_apps.plugins.builtin.telegram.settings.reply_to_message": "Reply directly to the source Telegram message when possible.",
    "interactive_apps.plugins.builtin.telegram.settings.topic_scoped_sessions": "Scope Telegram sessions by topic when present.",
    "interactive_apps.plugins.builtin.telegram.settings.drop_pending_updates": "Drop stale Telegram updates on startup.",
    "interactive_apps.plugins.builtin.telegram.settings.rich_messages": "Send Telegram replies through Bot API rich messages when supported.",
    "interactive_apps.plugins.builtin.telegram.settings.connection_pool_size": "Telegram HTTP connection pool size.",
    "interactive_apps.plugins.builtin.telegram.settings.concurrent_updates": "Maximum Telegram updates processed concurrently.",
    "interactive_apps.plugins.builtin.telegram.settings.pool_timeout": "Telegram HTTP connection-pool timeout in seconds.",
    "interactive_apps.plugins.builtin.discord.settings.enabled": "Enable the Discord interactive adapter.",
    "interactive_apps.plugins.builtin.discord.settings.allowed_user_ids": "Discord user ids allowed to contact the team.",
    "interactive_apps.plugins.builtin.discord.settings.allowed_channel_ids": "Discord channel ids allowed to contact the team.",
    "interactive_apps.plugins.builtin.discord.settings.group_policy": "Discord group handling policy: mention or open.",
    "interactive_apps.plugins.builtin.discord.settings.reply_to_message": "Reply directly to the source Discord message when possible.",
    "interactive_apps.plugins.builtin.discord.settings.thread_scoped_sessions": "Scope Discord sessions by thread when present.",
    "interactive_apps.plugins.builtin.discord.settings.command_guild_ids": "Optional guild-id strings for command sync.",
    "interactive_apps.plugins.builtin.whatsapp.settings.enabled": "Enable the Twilio-backed WhatsApp adapter.",
    "interactive_apps.plugins.builtin.whatsapp.settings.allowed_sender_ids": "WhatsApp sender allowlist. Empty means no sender allowlist.",
    "interactive_apps.plugins.builtin.whatsapp.settings.webhook_host": "Listener host for Twilio webhook callbacks.",
    "interactive_apps.plugins.builtin.whatsapp.settings.webhook_port": "Listener port for Twilio webhook callbacks.",
    "interactive_apps.plugins.builtin.whatsapp.settings.webhook_path": "Twilio webhook path.",
    "interactive_apps.plugins.builtin.whatsapp.settings.public_base_url": "Public base URL used for Twilio signature validation.",
    "interactive_apps.plugins.builtin.whatsapp.settings.validate_twilio_signature": "Validate inbound Twilio signatures.",
    "interactive_apps.plugins.builtin.signal.settings.enabled": "Enable the Signal interactive adapter.",
    "interactive_apps.plugins.builtin.signal.settings.base_url": "Base URL for the authenticated signal-cli-rest-api server.",
    "interactive_apps.plugins.builtin.signal.settings.account": "Registered Signal account or number used by the REST server.",
    "interactive_apps.plugins.builtin.signal.settings.allowed_sender_ids": "Signal direct sender allowlist. Empty means no direct sender allowlist.",
    "interactive_apps.plugins.builtin.signal.settings.allowed_group_ids": "Signal group id allowlist. Empty disables group handling.",
    "interactive_apps.plugins.builtin.signal.settings.group_policy": "Signal group handling policy: mention or open.",
    "interactive_apps.plugins.builtin.signal.settings.mention_markers": "Case-insensitive markers required in Signal groups when group_policy is mention.",
    "interactive_apps.plugins.builtin.signal.settings.poll_timeout_seconds": "Signal receive long-poll timeout in seconds.",
    "interactive_apps.plugins.builtin.signal.settings.poll_interval_seconds": "Delay between empty Signal receive polls in seconds.",
    "interactive_apps.plugins.builtin.signal.settings.max_messages_per_poll": "Maximum Signal messages requested per receive poll.",
    "interactive_apps.plugins.builtin.signal.settings.request_timeout_seconds": "Signal REST request timeout in seconds.",
    "interactive_apps.plugins.builtin.signal.settings.ignore_attachments": "Ask signal-cli-rest-api not to download attachments on receive.",
    "interactive_apps.plugins.builtin.signal.settings.ignore_stories": "Ignore Signal stories on receive.",
    "interactive_apps.plugins.builtin.signal.settings.ignore_avatars": "Ignore Signal avatars on receive.",
    "interactive_apps.plugins.builtin.signal.settings.ignore_stickers": "Ignore Signal stickers on receive.",
    "interactive_apps.plugins.builtin.signal.settings.send_read_receipts": "Send Signal read receipts when receiving messages.",
    "interactive_apps.plugins.builtin.signal.settings.drop_pending_messages_on_startup": "Drop queued Signal receive messages when the adapter starts.",
    "interactive_apps.plugins.builtin.signal.settings.text_mode": "Signal outbound text mode for /v2/send.",
    "interactive_apps.plugins.builtin.photon.settings.enabled": "Enable the Photon Spectrum iMessage adapter.",
    "interactive_apps.plugins.builtin.photon.settings.project_id": "Photon Spectrum project id. If empty, project_id_env_var is used.",
    "interactive_apps.plugins.builtin.photon.settings.project_id_env_var": "Env var containing the Photon Spectrum project id.",
    "interactive_apps.plugins.builtin.photon.settings.project_secret_env_var": "Env var containing the Photon Spectrum project secret; keep the value in team.env or a secret file.",
    "interactive_apps.plugins.builtin.photon.settings.sidecar_token_env_var": "Optional env var containing a pre-shared Photon sidecar token.",
    "interactive_apps.plugins.builtin.photon.settings.sidecar_bind_host": "Loopback bind host for the Photon sidecar.",
    "interactive_apps.plugins.builtin.photon.settings.sidecar_port": "Loopback port for the Photon sidecar.",
    "interactive_apps.plugins.builtin.photon.settings.sidecar_autostart": "Start the bundled Node sidecar with the adapter.",
    "interactive_apps.plugins.builtin.photon.settings.node_bin": "Node.js executable used to launch the Photon sidecar.",
    "interactive_apps.plugins.builtin.photon.settings.allowed_sender_ids": "Photon direct sender allowlist. Empty means no direct sender allowlist.",
    "interactive_apps.plugins.builtin.photon.settings.allowed_group_ids": "Photon group id allowlist. Empty disables group handling.",
    "interactive_apps.plugins.builtin.photon.settings.group_policy": "Photon group handling policy: mention or open.",
    "interactive_apps.plugins.builtin.photon.settings.mention_markers": "Case-insensitive markers required in Photon groups when group_policy is mention.",
    "interactive_apps.plugins.builtin.photon.settings.request_timeout_seconds": "Photon sidecar HTTP request timeout in seconds.",
    "interactive_apps.plugins.builtin.photon.settings.max_inline_attachment_bytes": "Maximum Photon inbound attachment bytes that the sidecar may inline.",
    "interactive_apps.plugins.builtin.photon.settings.markdown": "Send Photon outbound text through Spectrum markdown.",
    "interactive_apps.plugins.builtin.photon.settings.telemetry": "Enable Spectrum telemetry in the Photon sidecar.",
    "interactive_apps_cli": "CLI-facing app defaults.",
    "otel": "OpenTelemetry tracing and metrics settings.",
    "otel.enabled": "Master OTEL switch.",
    "otel.enable_traces": "Export traces.",
    "otel.enable_metrics": "Export metrics.",
    "otel.otlp_endpoint": "OTLP endpoint.",
    "otel.service_name": "OTEL service name.",
    "otel.headers": "Optional non-secret OTEL headers.",
}


def _humanize_config_key(key: str) -> str:
    return key.replace("_", " ").replace("-", " ").strip().capitalize()


def _instance_config_repeated_comment(path: str) -> Optional[str]:
    if path.startswith("interactive_apps.plugins."):
        key = path.rsplit(".", 1)[-1]
        return {
            "distribution": (
                "Exact normalized distribution approved for this optional plugin."
            ),
            "execution": "Execution owner: in_process or the authenticated adapter_host process.",
            "adapter_instance_id": "Stable adapter stream id; omitted derives it from instance_name and plugin id.",
            "settings": "Plugin-owned non-secret settings validated by its descriptor.",
            "enabled": "Enable discovery and lifecycle for this interactive app plugin.",
        }.get(key)
    if path.startswith("agent.memory.plugins."):
        key = path.rsplit(".", 1)[-1]
        return {
            "enabled": "Enable this memory plugin.",
            "priority": "Memory plugin rendering priority.",
            "pinned": "Pin this memory plugin output during context budgeting.",
            "degradable": "Allow this memory plugin output to be omitted during context pressure.",
        }.get(key)
    if path.startswith("agent.inner_loop_context.filters."):
        key = path.rsplit(".", 1)[-1]
        return {
            "enabled": "Enable this historical tool-output filter.",
            "trigger_tokens": "Token threshold that activates this historical tool-output filter.",
            "comms_json_lenient": "Use lenient handling for comms-shaped JSON.",
            "comms_json_trigger_tokens": "Higher token threshold for comms-shaped JSON.",
        }.get(key)
    if path.startswith("agent_types."):
        key = path.rsplit(".", 1)[-1]
        if key == "agent":
            return "Agent runtime overrides for this agent type."
        if key == "llm":
            return "LLM overrides for this agent type."
        if key == "prompt_skill_description_exclusions":
            return "Skill names hidden from this agent type's AgentSkills menu/XML prompt content."
        if key == "prompt_skill_description_limit":
            return "Maximum deduplicated skill descriptions shown for this agent type; omitted skills remain activatable."
        if key == "external_mcp_servers":
            return "Static MCP servers attached to this agent type at startup."
        agent_type_depth = len(path.split("."))
        if agent_type_depth == 2:
            return "Per-agent-type runtime and LLM overrides."
    return None


def _instance_config_comment_for_path(path: str, *, is_block: bool) -> str:
    comment = _INSTANCE_CONFIG_COMMENTS.get(path)
    if comment:
        return comment
    repeated_comment = _instance_config_repeated_comment(path)
    if repeated_comment:
        return repeated_comment
    key = path.rsplit(".", 1)[-1]
    if is_block:
        return f"{_humanize_config_key(key)} settings."
    return f"{_humanize_config_key(key)} setting."


def _append_aligned_comment(line: str, *, indent: int, comment: str) -> str:
    target_column = _INSTANCE_CONFIG_COMMENT_COLUMNS.get(indent, max(80, indent + 40))
    padding = max(1, target_column - len(line))
    return f"{line}{' ' * padding}# {comment}"


def _annotate_instance_config_yaml(yaml_text: str) -> str:
    """Add operator-facing comments to generated instance_config.yaml content."""

    annotated: list[str] = []
    stack: list[tuple[int, str]] = []
    for raw_line in yaml_text.splitlines():
        stripped = raw_line.lstrip()
        if not stripped or stripped.startswith("#"):
            annotated.append(raw_line)
            continue
        indent = len(raw_line) - len(stripped)

        path: str
        is_block = False
        if stripped.startswith("- "):
            while stack and stack[-1][0] > indent:
                stack.pop()
            path = stack[-1][1] if stack else "item"
        else:
            while stack and stack[-1][0] >= indent:
                stack.pop()
            key, _, raw_value = stripped.partition(":")
            normalized_key = key.strip().strip("'\"")
            parent = stack[-1][1] if stack else ""
            path = f"{parent}.{normalized_key}" if parent else normalized_key
            is_block = raw_value.strip() == ""
            if is_block:
                stack.append((indent, path))

        comment = _instance_config_comment_for_path(path, is_block=is_block)
        annotated.append(
            _append_aligned_comment(raw_line, indent=indent, comment=comment)
        )
    return "\n".join(annotated) + "\n"


def dump_instance_config_yaml(data: Dict[str, Any]) -> str:
    """Render instance_config.yaml with aligned inline comments for operators."""

    yaml_text = yaml.safe_dump(
        _drop_incidental_config_nulls(data),
        sort_keys=False,
        width=4096,
    )
    return _annotate_instance_config_yaml(yaml_text)


def _drop_empty_agent_type_overrides(data: Dict[str, Any]) -> Dict[str, Any]:
    sanitized = dict(data)
    agent_types = dict(sanitized.get("agent_types") or {})
    retained: dict[str, Any] = {}
    for agent_type, settings in agent_types.items():
        settings_payload = dict(settings or {})
        agent_payload = dict(settings_payload.get("agent") or {})
        llm_payload = dict(settings_payload.get("llm") or {})
        if agent_payload:
            settings_payload["agent"] = agent_payload
        else:
            settings_payload.pop("agent", None)
        if llm_payload:
            settings_payload["llm"] = llm_payload
        else:
            settings_payload.pop("llm", None)
        if settings_payload:
            retained[agent_type] = settings_payload
    if retained:
        sanitized["agent_types"] = retained
    else:
        sanitized.pop("agent_types", None)
    return sanitized


def _resolve_env_postgres_dsn(
    *,
    configured_dsn: Optional[str],
    env: Optional[Mapping[str, str]] = None,
    setting_label: str,
    bundled: bool = False,
) -> Optional[str]:
    """The saved connection string, else POSTGRES_DSN.

    ``bundled`` databases (the manager's and the gateway's) move to the
    deployment's own PostgreSQL when it runs one (``BUNDLED_POSTGRES_ADDRESS_ENV``);
    that happens here, in memory, so the saved file keeps the operator's value.
    """
    env_mapping = env or os.environ
    dsn = _normalized_optional_string(configured_dsn)
    if not dsn:
        dsn = (
            get_env_with_file_fallback("POSTGRES_DSN", env=env_mapping)
            if env_mapping
            else None
        )
        if dsn:
            logger.info(
                "%s not set in instance_config.yaml; using POSTGRES_DSN from the environment.",
                setting_label,
            )
    return with_bundled_postgres_address(dsn, env_mapping) if bundled else dsn


class InstanceConfigFileError(ValueError):
    """instance_config.yaml cannot be read as YAML; the message names the place."""


def _read_instance_config_yaml(config_path: Path) -> Dict[str, Any]:
    return _parse_instance_config_yaml(config_path.read_bytes(), config_path)


def _parse_instance_config_yaml(content: bytes, config_path: Path) -> Dict[str, Any]:
    """Parse instance_config.yaml, reporting a broken file by line and column.

    Hand edits (for example pasting the theme settings Web Chat copies)
    most often break the file by indenting keys of the same level unevenly;
    a bare parser traceback does not tell the operator where to look.
    """

    try:
        data = yaml.safe_load(content.decode("utf-8")) or {}
    except yaml.YAMLError as exc:
        mark = getattr(exc, "problem_mark", None)
        place = f" at line {mark.line + 1}, column {mark.column + 1}" if mark is not None else ""
        problem = " ".join(
            part for part in (getattr(exc, "context", None), getattr(exc, "problem", None)) if part
        ) or str(exc)
        raise InstanceConfigFileError(
            f"{config_path} is not valid YAML{place}: {problem}. Keys at the same level must "
            "start at the same column; check the indentation around that line."
        ) from exc
    if not isinstance(data, dict):
        raise InstanceConfigFileError(f"{config_path} must hold a YAML mapping of settings.")
    return data


def _detect_instance_config_version(data: Dict[str, Any]) -> int:
    if "schema_version" not in data:
        raise ValueError("instance_config.yaml must declare schema_version")
    raw = data["schema_version"]
    if isinstance(raw, bool) or not isinstance(raw, int):
        raise ValueError(f"Invalid instance_config schema_version: {raw!r}")
    if raw < 1:
        raise ValueError(f"Invalid instance_config schema_version: {raw}")
    return raw


def inspect_instance_config_version(path: str | Path) -> InstanceConfigVersionStatus:
    config_path = Path(path).expanduser().resolve()
    data = _read_instance_config_yaml(config_path)
    detected_version = _detect_instance_config_version(data)
    if detected_version > INSTANCE_CONFIG_SCHEMA_VERSION:
        raise ValueError(
            f"instance_config schema_version {detected_version} is newer than supported latest "
            f"version {INSTANCE_CONFIG_SCHEMA_VERSION}"
        )
    return InstanceConfigVersionStatus(
        path=config_path,
        detected_version=detected_version,
        latest_version=INSTANCE_CONFIG_SCHEMA_VERSION,
        directly_compatible_versions=tuple(
            sorted(DIRECTLY_COMPATIBLE_INSTANCE_CONFIG_VERSIONS)
        ),
        needs_migration=detected_version
        not in DIRECTLY_COMPATIBLE_INSTANCE_CONFIG_VERSIONS,
        is_directly_compatible=detected_version
        in DIRECTLY_COMPATIBLE_INSTANCE_CONFIG_VERSIONS,
    )


def _migrate_instance_config_dict(
    data: Dict[str, Any],
    *,
    source_path: Path,
) -> tuple[Dict[str, Any], tuple[str, ...]]:
    """Validate the public instance-configuration baseline and apply future upgrades."""
    del source_path
    migrated = dict(data)
    version = _detect_instance_config_version(migrated)
    if version > INSTANCE_CONFIG_SCHEMA_VERSION:
        raise ValueError(
            f"instance_config schema_version {version} is newer than supported latest "
            f"version {INSTANCE_CONFIG_SCHEMA_VERSION}"
        )
    applied_steps: list[str] = []
    if version in (1, 2, 3):
        # These steps used to add settings of the old permission system
        # (authorization.permission_delegation selectors, emergency_deny,
        # enforced_targets). Those settings are refused by name now
        # (refuse_removed_access_settings), so the steps only advance the version.
        applied_steps.extend(
            f"{step} -> {step + 1}: no change (the settings it added were removed)"
            for step in range(version, 4)
        )
        migrated["schema_version"] = 4
        version = 4

    if version == 4:
        mcp = dict(migrated.get("mcp") or {})
        static_reconciliation = dict(mcp.get("static_reconciliation") or {})
        static_reconciliation.setdefault("upgrade_mode", "grandfather_current")
        mcp["static_reconciliation"] = static_reconciliation
        migrated["mcp"] = mcp
        migrated["schema_version"] = 5
        applied_steps.append(
            "4 -> 5: register the one-time baseline of existing MCP connections"
        )
        version = 5

    if version == 5:
        interactive_apps = dict(migrated.get("interactive_apps") or {})
        removed_apps = sorted(
            set(interactive_apps).intersection(
                {
                    "website",
                    "internal_website",
                    "discord",
                    "telegram",
                    "whatsapp",
                    "signal",
                    "photon",
                }
            )
        )
        if removed_apps:
            rendered = ", ".join(f"interactive_apps.{name}" for name in removed_apps)
            raise ValueError(
                f"{rendered} has been removed. Use "
                "interactive_apps.plugins.<plugin-id>.settings instead."
            )
        migrated["schema_version"] = 6
        applied_steps.append(
            "5 -> 6: require registry-keyed interactive app configuration"
        )
        version = 6

    if version == 6:
        interactive_apps = dict(migrated.get("interactive_apps") or {})
        interactive_apps.setdefault(
            "adapter_host",
            AdapterHostSettings().model_dump(mode="json"),
        )
        plugins = dict(interactive_apps.get("plugins") or {})
        for plugin_id, raw_entry in plugins.items():
            entry = dict(raw_entry or {})
            entry.setdefault("execution", INTERACTIVE_APP_EXECUTION_IN_PROCESS)
            entry.setdefault("adapter_instance_id", None)
            plugins[plugin_id] = entry
        interactive_apps["plugins"] = plugins
        migrated["interactive_apps"] = interactive_apps
        migrated["schema_version"] = 7
        applied_steps.append(
            "6 -> 7: add explicit authenticated adapter-host execution settings"
        )
        version = 7

    if version == 7:
        workspace = dict(migrated.get("workspace") or {})
        if "objective_execution_path" in workspace:
            raise ValueError(
                "workspace.objective_execution_path has been removed. Use the ordered "
                "workspace.objective_execution_paths array instead."
            )
        migrated["schema_version"] = 8
        applied_steps.append(
            "7 -> 8: require ordered bounded Objective execution policy paths"
        )
        version = 8

    if version == 8:
        if "automation_jobs" in migrated:
            raise ValueError(
                "automation_jobs cannot be migrated because mutable runnable paths and raw "
                "credential environment injection were removed. Recreate the workloads under "
                "execution_control using immutable artifacts and exact activations."
            )
        migrated["schema_version"] = 9
        applied_steps.append(
            "8 -> 9: require immutable execution-control artifacts and fenced run attempts"
        )
        version = 9

    if version == 9:
        _migrate_instance_to_layout(migrated)
        migrated["schema_version"] = 10
        applied_steps.append(
            "9 -> 10: derive control/, dbs/, state/ and workspaces/ from instance_root"
        )
        version = 10

    if version == 10:
        notes = move_worker_capacity_settings(migrated)
        migrated["schema_version"] = 11
        applied_steps.append(
            "10 -> 11: worker capacity moved to agent.worker_pools"
            + (f" ({'; '.join(notes)})" if notes else "")
        )
        version = 11

    if version == 11:
        # The MCP folder settings (mcp.registry_dir, mcp.hosting.definitions_dir,
        # mcp.hosting.state_dir) became fixed places under runtime/. A setting
        # still holding its default is dropped; a custom one is refused.
        drop_removed_location_settings(migrated)
        migrated["schema_version"] = 12
        applied_steps.append(
            "11 -> 12: MCP connection and hosting folders derived from instance_root"
        )
        version = 12

    return migrated, tuple(applied_steps)


def migrate_instance_config_file(
    path: str | Path,
    *,
    create_backup: bool = True,
) -> InstanceConfigMigrationResult:
    config_path = Path(path).expanduser().resolve()
    previous = config_path.read_bytes()
    raw = _parse_instance_config_yaml(previous, config_path)
    original_version = _detect_instance_config_version(raw)
    migrated, applied_steps = _migrate_instance_config_dict(
        raw, source_path=config_path
    )
    migrated_version = _detect_instance_config_version(migrated)

    backup_path: Optional[Path] = None
    if create_backup:
        backup_path = config_path.with_name(
            f"{config_path.name}.bak.v{original_version}"
        )

    persisted = _strip_persisted_instance_config_secrets(migrated)
    replace_config_file(
        config_path,
        dump_instance_config_yaml(persisted).encode("utf-8"),
        expected_revision=config_revision(previous),
        backup_path=backup_path,
    )
    if backup_path is not None:
        prune_config_backups(
            config_path,
            event_logger=logger,
            preserve_paths=(backup_path,),
        )
    return InstanceConfigMigrationResult(
        path=config_path,
        original_version=original_version,
        migrated_version=migrated_version,
        backup_path=backup_path,
        applied_steps=applied_steps,
    )


class GatewayStateSettings(BaseModel):
    storage: ManagerStorageSettings = Field(default_factory=ManagerStorageSettings)


class StartupAgentSettings(BaseModel):
    named_agent_type: str
    count: int = 1
    enabled: bool = True

    @field_validator("named_agent_type", mode="before")
    @classmethod
    def _normalize_named_agent_type(cls, value: Any) -> str:
        normalized = str(value or "").strip()
        if not normalized:
            raise ValueError(
                "gateway.startup_agents.named_agent_type must be non-empty."
            )
        return normalized

    @field_validator("count", mode="before")
    @classmethod
    def _normalize_count(cls, value: Any) -> int:
        try:
            parsed = int(value or 1)
        except (TypeError, ValueError) as exc:
            raise ValueError(
                "gateway.startup_agents.count must be an integer."
            ) from exc
        if parsed < 1 or parsed > 32:
            raise ValueError("gateway.startup_agents.count must be between 1 and 32.")
        return parsed


def ensure_required_startup_agents(
    startup_agents: list[StartupAgentSettings] | None,
) -> list[StartupAgentSettings]:
    agents = list(startup_agents or [])
    for setting in agents:
        if setting.named_agent_type == MEM_REVIEW_AGENT_TYPE and setting.enabled:
            return agents
    return [
        *agents,
        StartupAgentSettings(
            named_agent_type=MEM_REVIEW_AGENT_TYPE,
            count=1,
            enabled=True,
        ),
    ]


def _default_startup_agents() -> list[StartupAgentSettings]:
    return [StartupAgentSettings(named_agent_type=MEM_REVIEW_AGENT_TYPE, count=1)]


class GatewayUsersSettings(BaseModel):
    enabled: bool = GATEWAY_DEFAULTS.users_enabled.value
    allow_newuser_registration: bool = (
        GATEWAY_DEFAULTS.users_allow_newuser_registration.value
    )
    link_code_ttl_seconds: int = GATEWAY_DEFAULTS.users_link_code_ttl_seconds.value
    web_user_invite_ttl_seconds: int = (
        GATEWAY_DEFAULTS.users_web_user_invite_ttl_seconds.value
    )
    web_user_tokens_enabled: bool = GATEWAY_DEFAULTS.users_web_user_tokens_enabled.value
    web_user_token_ttl_days: int = GATEWAY_DEFAULTS.users_web_user_token_ttl_days.value
    removed_data_retention_days: int = (
        GATEWAY_DEFAULTS.users_removed_data_retention_days.value
    )

    @model_validator(mode="before")
    @classmethod
    def _reject_removed_team_model_keys_setting(cls, value: Any) -> Any:
        # Whether people you add use the team's model keys is the team's
        # model-key sharing in the access store now (access_control.account_sharing),
        # which administrators change from chat without a restart. Refused
        # here so the instance config and an onboarding base config both say so.
        if isinstance(value, Mapping) and REMOVED_TEAM_MODEL_KEYS_SETTING[-1] in value:
            raise ValueError(
                f"{'.'.join(REMOVED_TEAM_MODEL_KEYS_SETTING)} has been removed. Delete it from instance_config.yaml "
                "(or from the settings file onboarding imports). "
                "Whether people you add may use the team's model keys is now the team's model-key sharing: "
                f"after the assistant starts, an administrator sends {accounts_command(ACCOUNTS_SHARE, ACCOUNTS_MODEL_SUBJECT)} "
                f"or {accounts_command(ACCOUNTS_PRIVATE, ACCOUNTS_MODEL_SUBJECT)} "
                "(shared until someone chooses). Each person's own switch stays: /user team-keys."
            )
        return value

    @field_validator(
        "link_code_ttl_seconds",
        "web_user_invite_ttl_seconds",
        "web_user_token_ttl_days",
        "removed_data_retention_days",
        mode="before",
    )
    @classmethod
    def _normalize_non_negative_int(cls, value: Any, info: Any) -> int:
        try:
            parsed = int(value)
        except (TypeError, ValueError) as exc:
            raise ValueError(
                f"gateway.users.{info.field_name} must be an integer."
            ) from exc
        if parsed < 0:
            raise ValueError(f"gateway.users.{info.field_name} must be >= 0.")
        if info.field_name == "web_user_invite_ttl_seconds" and parsed < 60:
            raise ValueError("gateway.users.web_user_invite_ttl_seconds must be >= 60.")
        if info.field_name == "web_user_invite_ttl_seconds" and parsed > 86400:
            raise ValueError(
                "gateway.users.web_user_invite_ttl_seconds must be <= 86400."
            )
        return parsed


def _whole_number(value: Any, *, name: str) -> int:
    """An int, or a string of digits, as a whole number; refuses true/false and fractions."""

    if isinstance(value, int) and not isinstance(value, bool):
        return value
    if isinstance(value, str) and value.strip().isdigit():
        return int(value.strip())
    raise ValueError(f"{name} must be a whole number.")


class StorageWarningSettings(BaseModel):
    """When people hear that the data the team stores for them has grown (``assistant_gateway.storage_usage``)."""

    person_step_gb: int = GATEWAY_DEFAULTS.storage_warnings_person_step_gb.value
    nearing_limit_percent: int = GATEWAY_DEFAULTS.storage_warnings_nearing_limit_percent.value
    nearly_full_percent: int = GATEWAY_DEFAULTS.storage_warnings_nearly_full_percent.value

    @field_validator("person_step_gb", mode="before")
    @classmethod
    def _step_in_range(cls, value: Any) -> int:
        name = "gateway.storage_warnings.person_step_gb"
        parsed = _whole_number(value, name=name)
        if not 0 <= parsed <= MAX_STORAGE_WARNING_STEP_GB:
            raise ValueError(f"{name} must be from 0 (no notices) to {MAX_STORAGE_WARNING_STEP_GB}.")
        return parsed

    @field_validator("nearing_limit_percent", "nearly_full_percent", mode="before")
    @classmethod
    def _percent_in_range(cls, value: Any, info: Any) -> int:
        name = f"gateway.storage_warnings.{info.field_name}"
        parsed = _whole_number(value, name=name)
        if not 1 <= parsed <= 100:
            raise ValueError(f"{name} must be from 1 to 100.")
        return parsed

    @model_validator(mode="after")
    def _warning_before_problem(self) -> "StorageWarningSettings":
        if self.nearing_limit_percent >= self.nearly_full_percent:
            raise ValueError(
                "gateway.storage_warnings.nearing_limit_percent must be below nearly_full_percent, "
                "so administrators are warned before storage is nearly full."
            )
        return self


class ManagerTargetSettings(BaseModel):
    team_id: str = AGENT_DEFAULTS.default_team_id.value
    named_agent_type: str = AGENT_DEFAULTS.named_agent_type.value
    manager_agent_id: Optional[str] = None
    request_timeout_seconds: float = (
        GATEWAY_DEFAULTS.manager_request_timeout_seconds.value
    )
    response_queue_name: str = ""
    startup_agents: list[StartupAgentSettings] = Field(
        default_factory=_default_startup_agents
    )
    storage: StateStorageSettings = Field(default_factory=StateStorageSettings)
    users: GatewayUsersSettings = Field(default_factory=GatewayUsersSettings)
    storage_warnings: StorageWarningSettings = Field(default_factory=StorageWarningSettings)

    @field_validator("startup_agents")
    @classmethod
    def _ensure_required_startup_agents(
        cls,
        value: list[StartupAgentSettings],
    ) -> list[StartupAgentSettings]:
        return ensure_required_startup_agents(value)

    @field_validator("response_queue_name", mode="before")
    @classmethod
    def _normalize_response_queue_name(cls, value: Any) -> str:
        return str(value or "").strip()


class AssistantProfileSettings(BaseModel):
    display_name: str = GATEWAY_DEFAULTS.assistant_display_name.value
    manager_transition_notices_enabled: bool = (
        DEFAULT_MANAGER_TRANSITION_NOTICES_ENABLED
    )
    unavailable_message_template: str = DEFAULT_UNAVAILABLE_MESSAGE_TEMPLATE
    available_message_template: str = DEFAULT_AVAILABLE_MESSAGE_TEMPLATE


class UserProfileSettings(BaseModel):
    display_name: str = GATEWAY_DEFAULTS.user_display_name.value
    canonical_user_id: str = GATEWAY_DEFAULTS.user_display_name.value
    timezone: str = GATEWAY_DEFAULTS.user_timezone.value
    default_session_key: str = GATEWAY_DEFAULTS.default_session_key.value
    role: str = ""
    initial_context: str = ""

    @model_validator(mode="after")
    def _default_canonical_user_id(self) -> "UserProfileSettings":
        if not self.canonical_user_id.strip():
            self.canonical_user_id = (
                self.display_name.strip() or GATEWAY_DEFAULTS.user_display_name.value
            )
        return self


class QuietHoursSettings(BaseModel):
    enabled: bool = DELIVERY_DEFAULTS.quiet_hours_enabled.value
    start_hour_local: int = DELIVERY_DEFAULTS.quiet_start_hour_local.value
    end_hour_local: int = DELIVERY_DEFAULTS.quiet_end_hour_local.value
    allow_urgent_delivery: bool = DELIVERY_DEFAULTS.allow_urgent_delivery.value

    @field_validator("start_hour_local", "end_hour_local")
    @classmethod
    def _validate_hour(cls, value: int) -> int:
        if value < 0 or value > 23:
            raise ValueError("Hour values must be between 0 and 23.")
        return value


class DeliveryTargetSettings(BaseModel):
    channel_name: str = GATEWAY_DEFAULTS.delivery_channel_name.value
    chat_id: str = ""
    thread_id: str = ""

    @field_validator("channel_name", "chat_id", "thread_id", mode="before")
    @classmethod
    def _coerce_delivery_strings(cls, value: Any) -> str:
        if value is None:
            return ""
        return str(value)

    @model_validator(mode="after")
    def _default_channel_name(self) -> "DeliveryTargetSettings":
        normalized_channel_name = self.channel_name.strip()
        self.channel_name = (
            normalized_channel_name or GATEWAY_DEFAULTS.delivery_channel_name.value
        )
        return self


class NotificationRoutingSettings(BaseModel):
    mode: str = DELIVERY_DEFAULTS.notification_routing_mode.value

    @field_validator("mode", mode="before")
    @classmethod
    def _normalize_mode(cls, value: Any) -> str:
        rendered = (
            str(value or DELIVERY_DEFAULTS.notification_routing_mode.value)
            .strip()
            .lower()
        )
        aliases = {
            "auto": "source_thread_or_default",
            "source_thread": "source_thread_or_default",
            "source_thread_when_interactive": "source_thread_or_default",
            "preferred": "default_target",
            "preferred_profile": "default_target",
            "default": "default_target",
        }
        normalized = aliases.get(rendered, rendered)
        if normalized not in {"source_thread_or_default", "default_target"}:
            raise ValueError(
                "delivery.notification_routing.mode must be "
                "'source_thread_or_default' or 'default_target'."
            )
        return normalized


class DeliverySettings(BaseModel):
    quiet_hours: QuietHoursSettings = Field(default_factory=QuietHoursSettings)
    digest_delivery_hour_local: int = DELIVERY_DEFAULTS.digest_delivery_hour_local.value
    default_target: DeliveryTargetSettings = Field(
        default_factory=DeliveryTargetSettings
    )
    notification_routing: NotificationRoutingSettings = Field(
        default_factory=NotificationRoutingSettings
    )
    summarize_deferred: bool = DELIVERY_DEFAULTS.summarize_deferred.value
    urgent_keywords: list[str] = Field(
        default_factory=lambda: list(DELIVERY_DEFAULTS.urgent_keywords.value),
    )

    @field_validator("digest_delivery_hour_local")
    @classmethod
    def _validate_digest_hour(cls, value: int) -> int:
        if value < 0 or value > 23:
            raise ValueError("Digest delivery hour must be between 0 and 23.")
        return value


class HeartbeatSettings(BaseModel):
    enabled: bool = HEARTBEAT_DEFAULTS.enabled.value
    interval_seconds: int = HEARTBEAT_DEFAULTS.interval_seconds.value
    session_key: str = HEARTBEAT_DEFAULTS.session_key.value
    source_id: Optional[str] = None
    schedule_id: Optional[str] = None
    active_hours_enabled: bool = HEARTBEAT_DEFAULTS.active_hours_enabled.value
    active_start_hour_local: int = HEARTBEAT_DEFAULTS.active_start_hour_local.value
    active_end_hour_local: int = HEARTBEAT_DEFAULTS.active_end_hour_local.value

    @field_validator("active_start_hour_local", "active_end_hour_local")
    @classmethod
    def _validate_active_hour(cls, value: int) -> int:
        if value < 0 or value > 23:
            raise ValueError("Heartbeat active-hour values must be between 0 and 23.")
        return value


class LaunchSettings(BaseModel):
    env_file_path: Optional[str] = None
    # env_file: secrets live in env_file_path (team.env). environment: only the
    # process environment and Docker secrets (*_FILE) hold them; no env file is
    # written or read (runtime_settings.secrets_source).
    secrets_source: SecretsSource = cast(SecretsSource, LAUNCH_DEFAULTS.secrets_source.value)

    @field_validator("secrets_source", mode="before")
    @classmethod
    def _normalize_secrets_source(cls, value: Any) -> str:
        return normalize_secrets_source(value)

    @model_validator(mode="after")
    def _no_env_file_when_secrets_come_from_environment(self) -> "LaunchSettings":
        # Two sources of the same secrets drift apart; refuse the mix by name.
        if self.secrets_from_environment and self.env_file_path:
            raise ValueError(
                "launch.env_file_path must be empty when launch.secrets_source is "
                "environment: secrets then come only from environment variables and "
                "Docker secrets. Remove launch.env_file_path, or set "
                "launch.secrets_source to env_file."
            )
        return self

    @property
    def secrets_from_environment(self) -> bool:
        return self.secrets_source == SECRETS_SOURCE_ENVIRONMENT


class GatewayInstanceConfig(BaseModel):
    _config_path: Optional[Path] = PrivateAttr(default=None)
    _source_revision: str | None = PrivateAttr(default=None)

    schema_version: int = INSTANCE_CONFIG_SCHEMA_VERSION
    instance_name: str
    instance_root: str
    state: GatewayStateSettings = Field(default_factory=GatewayStateSettings)
    transport: TransportSettings = Field(default_factory=TransportSettings)
    gateway: ManagerTargetSettings = Field(default_factory=ManagerTargetSettings)
    agent: AgentRuntimeDefaultsSettings = Field(
        default_factory=AgentRuntimeDefaultsSettings
    )
    workspace: WorkspaceSettings = Field(default_factory=WorkspaceSettings)
    agent_types: Dict[str, AgentTypeRuntimeSettings] = Field(default_factory=dict)
    assistant: AssistantProfileSettings = Field(
        default_factory=AssistantProfileSettings
    )
    user: UserProfileSettings = Field(default_factory=UserProfileSettings)
    network: NetworkSettings = Field(default_factory=NetworkSettings)
    delivery: DeliverySettings = Field(default_factory=DeliverySettings)
    heartbeat: HeartbeatSettings = Field(default_factory=HeartbeatSettings)
    llm: InstanceLLMSettings = Field(default_factory=InstanceLLMSettings)
    embedding: InstanceEmbeddingSettings = Field(
        default_factory=InstanceEmbeddingSettings
    )
    launch: LaunchSettings = Field(default_factory=LaunchSettings)
    tool_policy: ToolPolicySettings = Field(default_factory=ToolPolicySettings)
    authorization: AuthorizationSettings = Field(default_factory=AuthorizationSettings)
    access_workflows: AccessWorkflowSettings = Field(default_factory=AccessWorkflowSettings)
    security_layer: SecurityLayerSettings = Field(default_factory=SecurityLayerSettings)
    interactive_apps: InteractiveAppsSettings = Field(
        default_factory=InteractiveAppsSettings
    )
    voice: VoiceSettings = Field(default_factory=VoiceSettings)
    activity_collector: ActivityCollectorSettings = Field(
        default_factory=ActivityCollectorSettings
    )
    otel: OTelSettings = Field(default_factory=OTelSettings)
    execution_control: ExecutionControlSettings = Field(
        default_factory=ExecutionControlSettings
    )
    web_services: WebServicesSettings = Field(default_factory=WebServicesSettings)
    mcp: MCPSettings = Field(default_factory=MCPSettings)
    ui: UISettings = Field(default_factory=UISettings)
    mcp_profiles: Dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="before")
    @classmethod
    def _reject_legacy_top_level_workspace_dir(cls, value: Any) -> Any:
        if not isinstance(value, dict):
            return value
        agent = value.get("agent")
        removed = [
            *(["workspace_dir"] if "workspace_dir" in value else []),
            *(
                ["agent.workspace_dir"]
                if isinstance(agent, dict) and "workspace_dir" in agent
                else []
            ),
            *present_removed_location_settings(value),
        ]
        if removed:
            raise ValueError(
                f"{', '.join(removed)} has been removed. Every location is derived from "
                "instance_root (control/, dbs/, state/, runtime/, workspaces/)."
            )
        return value

    @model_validator(mode="before")
    @classmethod
    def _reject_removed_access_settings(cls, value: Any) -> Any:
        # The old permission system's settings (authorization.permission_delegation,
        # emergency_deny, human_interactions, ...); named one by one.
        refuse_removed_access_settings(value)
        return value

    @model_validator(mode="before")
    @classmethod
    def _reject_removed_automation_jobs(cls, value: Any) -> Any:
        if isinstance(value, dict) and "automation_jobs" in value:
            raise ValueError(
                "Top-level automation_jobs has been removed. Recreate immutable artifacts, "
                "revisions, and schedules under execution_control."
            )
        return value

    @model_validator(mode="before")
    @classmethod
    def _reject_removed_manager_block(cls, value: Any) -> Any:
        if not isinstance(value, dict):
            return value
        if "manager" in value:
            raise ValueError(
                "Top-level manager has been removed. Use gateway.* settings instead."
            )
        return value

    @model_validator(mode="before")
    @classmethod
    def _reject_removed_agent_default_block(cls, value: Any) -> Any:
        if not isinstance(value, dict):
            return value
        if "agent_default" in value:
            raise ValueError(
                "Top-level agent_default has been removed. Use agent.* settings instead."
            )
        return value

    @model_validator(mode="before")
    @classmethod
    def _reject_removed_docs_and_website_config(cls, value: Any) -> Any:
        if not isinstance(value, dict):
            return value
        workspace = value.get("workspace")
        if isinstance(workspace, dict) and "task_history_db_path" in workspace:
            raise ValueError(
                "workspace.task_history_db_path has been removed. Use state.storage.sqlite.path instead."
            )
        if "user_docs" in value:
            raise ValueError(
                "Top-level user_docs has been removed. Use interactive_apps.plugins.builtin.internal_website.settings.* settings instead."
            )
        security_layer = value.get("security_layer")
        if (
            isinstance(security_layer, dict)
            and "broker_public_base_url" in security_layer
        ):
            raise ValueError(
                "security_layer.broker_public_base_url has been removed. Use "
                "security_layer.broker_public_port with network.public_origin instead."
            )
        interactive_apps = value.get("interactive_apps")
        if isinstance(interactive_apps, dict) and "website" in interactive_apps:
            raise ValueError(
                "interactive_apps.website has been removed. Use interactive_apps.plugins.builtin.internal_website.settings instead."
            )
        return value

    @property
    def instance_root_path(self) -> Path:
        return Path(self.instance_root).expanduser().resolve()

    @property
    def layout(self) -> InstanceLayout:
        return InstanceLayout.at(self.instance_root_path)

    @property
    def workspace_path(self) -> Path:
        """The agents' area: ``workspaces/`` with ``users/``, ``team/``, ``agents/``."""
        return self.layout.workspaces_dir

    @property
    def state_db_file(self) -> Path:
        if self.gateway.storage.backend != STORAGE_BACKEND_SQLITE:
            raise RuntimeError(
                "state_db_file is only available when gateway.storage.backend=sqlite"
            )
        return Path(self.gateway.storage.sqlite.path or "").expanduser().resolve()

    @property
    def config_path(self) -> Path:
        if self._config_path is not None:
            return self._config_path
        return self.layout.instance_config_path

    @property
    def manager(self) -> ManagerTargetSettings:
        """Backward-compatible code alias for the gateway target settings."""
        return self.gateway

    @property
    def internal_website_base_url(self) -> str:
        return compose_public_base_url(
            self.network.public_origin,
            self.interactive_apps.internal_website.public_port,
        )

    @property
    def secure_tool_broker_public_base_url(self) -> str:
        return compose_secure_tool_broker_public_base_url(self)

    def resolve_manager_storage_settings(
        self,
        *,
        env: Optional[Mapping[str, str]] = None,
    ) -> StorageSettings:
        storage_cfg = self.state.storage.model_dump(mode="json")
        if self.state.storage.backend == STORAGE_BACKEND_POSTGRES:
            postgres_cfg = dict(storage_cfg.get(STORAGE_BACKEND_POSTGRES) or {})
            effective_dsn = _resolve_env_postgres_dsn(
                configured_dsn=postgres_cfg.get("dsn"),
                env=env,
                setting_label="state.storage.postgres.dsn",
                bundled=True,
            )
            if not effective_dsn:
                raise ValueError(
                    "state.storage.postgres.dsn is required when state.storage.backend=postgres "
                    "unless POSTGRES_DSN is set in the environment"
                )
            postgres_cfg["dsn"] = effective_dsn
            storage_cfg[STORAGE_BACKEND_POSTGRES] = postgres_cfg
        return resolve_storage_settings(
            storage_cfg,
            base_dir=str(self.instance_root_path),
            sqlite_fallback_path=DEFAULT_SQLITE_STORAGE_PATH,
        )

    def resolve_state_storage_settings(
        self,
        *,
        env: Optional[Mapping[str, str]] = None,
    ) -> StorageSettings:
        storage_cfg = self.gateway.storage.model_dump(mode="json")
        if self.gateway.storage.backend == STORAGE_BACKEND_POSTGRES:
            postgres_cfg = dict(storage_cfg.get(STORAGE_BACKEND_POSTGRES) or {})
            effective_dsn = _resolve_env_postgres_dsn(
                configured_dsn=postgres_cfg.get("dsn")
                or self.state.storage.postgres.dsn,
                env=env,
                setting_label="gateway.storage.postgres.dsn",
                bundled=True,
            )
            if not effective_dsn:
                raise ValueError(
                    "gateway.storage.postgres.dsn is required when gateway.storage.backend=postgres "
                    "unless POSTGRES_DSN is set in the environment"
                )
            postgres_cfg["dsn"] = effective_dsn
            storage_cfg[STORAGE_BACKEND_POSTGRES] = postgres_cfg
        return resolve_storage_settings(
            storage_cfg,
            base_dir=str(self.instance_root_path),
            sqlite_fallback_path=STORAGE_DEFAULTS.gateway_state_db_path.value,
        )

    def resolve_activity_collector_storage_settings(
        self,
        *,
        env: Optional[Mapping[str, str]] = None,
    ) -> StorageSettings:
        storage_cfg = self.activity_collector.storage.model_dump(mode="json")
        if self.activity_collector.storage.backend == STORAGE_BACKEND_POSTGRES:
            postgres_cfg = dict(storage_cfg.get(STORAGE_BACKEND_POSTGRES) or {})
            effective_dsn = _resolve_env_postgres_dsn(
                configured_dsn=postgres_cfg.get("dsn"),
                env=env,
                setting_label="activity_collector.storage.postgres.dsn",
            )
            if not effective_dsn:
                raise ValueError(
                    "activity_collector.storage.postgres.dsn is required when activity_collector.storage.backend=postgres "
                    "unless POSTGRES_DSN is set in the environment"
                )
            postgres_cfg["dsn"] = effective_dsn
            storage_cfg[STORAGE_BACKEND_POSTGRES] = postgres_cfg
        return resolve_storage_settings(
            storage_cfg,
            base_dir=str(self.instance_root_path),
            sqlite_fallback_path=STORAGE_DEFAULTS.activity_collector_db_path.value,
        )

    @property
    def manager_storage_settings(self) -> StorageSettings:
        return self.resolve_manager_storage_settings(env=os.environ)

    @property
    def state_storage_settings(self) -> StorageSettings:
        return self.resolve_state_storage_settings(env=os.environ)

    @property
    def activity_collector_storage_settings(self) -> StorageSettings:
        return self.resolve_activity_collector_storage_settings(env=os.environ)

    @property
    def broker_credentials_path(self) -> Path:
        from secure_tool_broker.credential_state import resolve_broker_credentials_dir

        return resolve_broker_credentials_dir(
            self.security_layer.broker_credentials_dir,
            instance_root=self.instance_root_path,
        )

    @property
    def credential_catalog_database_path(self) -> Path:
        return Path(
            _resolve_instance_path(
                self.security_layer.credential_catalog.database_path,
                instance_root_path=self.instance_root_path,
            )
        )

    @property
    def credential_template_overrides_path(self) -> Path:
        return Path(
            _resolve_instance_path(
                self.security_layer.credential_catalog.template_overrides_path,
                instance_root_path=self.instance_root_path,
            )
        )

    @model_validator(mode="after")
    def _normalize_gateway_response_route(self) -> "GatewayInstanceConfig":
        if not self.gateway.response_queue_name:
            self.gateway.response_queue_name = (
                f"{self.gateway.team_id}_{self.instance_name}_user_gateway"
            )
        return self

    @model_validator(mode="after")
    def _normalize_manager_storage_settings(self) -> "GatewayInstanceConfig":
        if self.state.storage.backend == STORAGE_BACKEND_SQLITE:
            self.state.storage.sqlite.path = _resolve_schedule_db_path(
                self.state.storage.sqlite.path or DEFAULT_SQLITE_STORAGE_PATH,
                instance_root_path=self.instance_root_path,
            )
        else:
            self.state.storage.postgres.dsn = _normalized_optional_string(
                self.state.storage.postgres.dsn
            )
        if self.gateway.storage.backend == STORAGE_BACKEND_SQLITE:
            self.gateway.storage.sqlite.path = _resolve_instance_path(
                self.gateway.storage.sqlite.path
                or STORAGE_DEFAULTS.gateway_state_db_path.value,
                instance_root_path=self.instance_root_path,
            )
        else:
            self.gateway.storage.postgres.dsn = _normalized_optional_string(
                self.gateway.storage.postgres.dsn
            )
        if self.activity_collector.storage.backend == STORAGE_BACKEND_SQLITE:
            self.activity_collector.storage.sqlite.path = _resolve_instance_path(
                self.activity_collector.storage.sqlite.path
                or STORAGE_DEFAULTS.activity_collector_db_path.value,
                instance_root_path=self.instance_root_path,
            )
        else:
            self.activity_collector.storage.postgres.dsn = _normalized_optional_string(
                self.activity_collector.storage.postgres.dsn
            )
        raw_env_path = self.launch.env_file_path
        if raw_env_path:
            resolved_env_path = Path(
                _resolve_instance_path(
                    raw_env_path,
                    instance_root_path=self.instance_root_path,
                )
            )
            if _is_path_within(self.workspace_path, resolved_env_path):
                raise ValueError(
                    "launch.env_file_path must live outside workspaces/."
                )
            self.launch.env_file_path = str(resolved_env_path)
        # The broker always runs, so its credential folder must always stay
        # out of the agents' area.
        if _is_path_within(self.workspace_path, self.broker_credentials_path):
            raise ValueError(
                "security_layer.broker_credentials_dir must live outside workspaces/."
            )
        return self

    @classmethod
    def create(
        cls,
        *,
        instance_name: str,
        instance_root: Optional[str] = None,
        team_id: str,
        timezone: str,
        response_queue_name: Optional[str] = None,
        manager_agent_id: Optional[str] = None,
        request_timeout_seconds: float = GATEWAY_DEFAULTS.manager_request_timeout_seconds.value,
        manager_storage_backend: str = STORAGE_DEFAULTS.backend.value,
        manager_storage_sqlite_path: Optional[str] = None,
        manager_storage_postgres_dsn: Optional[str] = None,
        state_storage_backend: Optional[str] = None,
        state_storage_sqlite_path: Optional[str] = None,
        state_storage_postgres_dsn: Optional[str] = None,
        heartbeat_enabled: bool = HEARTBEAT_DEFAULTS.enabled.value,
        heartbeat_interval_seconds: int = HEARTBEAT_DEFAULTS.interval_seconds.value,
        quiet_hours_enabled: bool = DELIVERY_DEFAULTS.quiet_hours_enabled.value,
        quiet_start_hour_local: int = DELIVERY_DEFAULTS.quiet_start_hour_local.value,
        quiet_end_hour_local: int = DELIVERY_DEFAULTS.quiet_end_hour_local.value,
        digest_delivery_hour_local: int = DELIVERY_DEFAULTS.digest_delivery_hour_local.value,
        display_name: str = GATEWAY_DEFAULTS.user_display_name.value,
        assistant_name: str = GATEWAY_DEFAULTS.assistant_display_name.value,
        named_agent_type: str = AGENT_DEFAULTS.named_agent_type.value,
        initial_user_context: str = "",
    ) -> "GatewayInstanceConfig":
        base_root = (
            Path(instance_root).expanduser()
            if instance_root
            else DEFAULT_INSTANCE_ROOT / instance_name
        ).resolve()
        normalized_storage_backend = (
            str(manager_storage_backend or STORAGE_DEFAULTS.backend.value)
            .strip()
            .lower()
        )
        if normalized_storage_backend == STORAGE_BACKEND_POSTGRESQL_ALIAS:
            normalized_storage_backend = STORAGE_BACKEND_POSTGRES
        normalized_state_storage_backend = (
            str(
                state_storage_backend
                or normalized_storage_backend
                or STORAGE_DEFAULTS.backend.value
            )
            .strip()
            .lower()
        )
        if normalized_state_storage_backend == STORAGE_BACKEND_POSTGRESQL_ALIAS:
            normalized_state_storage_backend = STORAGE_BACKEND_POSTGRES
        resolved_schedule_db_path = None
        if normalized_storage_backend == STORAGE_BACKEND_SQLITE:
            resolved_schedule_db_path = _resolve_schedule_db_path(
                manager_storage_sqlite_path or DEFAULT_SQLITE_STORAGE_PATH,
                instance_root_path=base_root,
            )
        resolved_state_db_path = None
        if normalized_state_storage_backend == STORAGE_BACKEND_SQLITE:
            resolved_state_db_path = _resolve_instance_path(
                state_storage_sqlite_path
                or STORAGE_DEFAULTS.gateway_state_db_path.value,
                instance_root_path=base_root,
            )
        response_queue = (
            response_queue_name or f"{team_id}_{instance_name}_user_gateway"
        )
        return cls(
            schema_version=INSTANCE_CONFIG_SCHEMA_VERSION,
            instance_name=instance_name,
            instance_root=str(base_root),
            state=GatewayStateSettings(
                storage=ManagerStorageSettings(
                    backend=normalized_storage_backend,
                    sqlite=ManagerSqliteStorageSettings(path=resolved_schedule_db_path),
                    postgres=ManagerPostgresStorageSettings(
                        dsn=manager_storage_postgres_dsn
                    ),
                )
            ),
            transport=TransportSettings(),
            gateway=ManagerTargetSettings(
                team_id=team_id,
                named_agent_type=named_agent_type,
                manager_agent_id=manager_agent_id,
                request_timeout_seconds=request_timeout_seconds,
                response_queue_name=response_queue,
                storage=StateStorageSettings(
                    backend=normalized_state_storage_backend,
                    sqlite=StateSqliteStorageSettings(path=resolved_state_db_path),
                    postgres=StatePostgresStorageSettings(
                        dsn=state_storage_postgres_dsn or manager_storage_postgres_dsn
                    ),
                ),
            ),
            agent=AgentRuntimeDefaultsSettings(),
            workspace=WorkspaceSettings(),
            assistant=AssistantProfileSettings(
                display_name=assistant_name,
            ),
            user=UserProfileSettings(
                display_name=display_name,
                canonical_user_id=display_name,
                timezone=timezone,
                initial_context=str(initial_user_context or "").strip(),
            ),
            delivery=DeliverySettings(
                quiet_hours=QuietHoursSettings(
                    enabled=quiet_hours_enabled,
                    start_hour_local=quiet_start_hour_local,
                    end_hour_local=quiet_end_hour_local,
                ),
                digest_delivery_hour_local=digest_delivery_hour_local,
            ),
            heartbeat=HeartbeatSettings(
                enabled=heartbeat_enabled,
                interval_seconds=heartbeat_interval_seconds,
                source_id=operational_id(HEARTBEAT_SOURCE_ID_PREFIX, instance_name),
                schedule_id=operational_id(
                    HEARTBEAT_SCHEDULE_ID_PREFIX,
                    instance_name,
                ),
            ),
        )

    def to_yaml_dict(self) -> Dict[str, Any]:
        sanitized = _strip_persisted_instance_config_secrets(
            self.model_dump(mode="json")
        )
        security_layer_payload = sanitized.get("security_layer")
        if isinstance(security_layer_payload, dict):
            explicit_security_fields = getattr(
                self.security_layer, "model_fields_set", set()
            )
            for field_name in (
                "broker_bind_host",
                "broker_bind_port",
                "broker_credentials_dir",
            ):
                if field_name not in explicit_security_fields:
                    security_layer_payload.pop(field_name, None)
        return _drop_empty_agent_type_overrides(
            _drop_incidental_config_nulls(sanitized)
        )

    @property
    def source_revision(self) -> str | None:
        """Revision of the exact bytes loaded or last saved, for checked replacement plans."""

        return self._source_revision

    def save(
        self,
        path: Optional[str | Path] = None,
        *,
        expected_revision: str | None = None,
        backup_path: Path | None = None,
    ) -> Path:
        """Save atomically, refusing to overwrite a change made since this model was loaded.

        A fresh model or a copy saved to another path creates a new file only.
        Intentional replacements pass the destination's previously read revision.
        """

        config_path = Path(path).expanduser().resolve() if path else self.config_path
        if expected_revision is None and config_path == self._config_path:
            expected_revision = self._source_revision
        self._source_revision = replace_config_file(
            config_path,
            dump_instance_config_yaml(self.to_yaml_dict()).encode("utf-8"),
            expected_revision=expected_revision,
            backup_path=backup_path,
        )
        self._config_path = config_path
        return config_path

    @classmethod
    def load(cls, path: str | Path) -> "GatewayInstanceConfig":
        config_path = Path(path).expanduser().resolve()
        content = config_path.read_bytes()
        data = _parse_instance_config_yaml(content, config_path)
        migrated, _ = _migrate_instance_config_dict(data, source_path=config_path)
        config = cls.model_validate(migrated)
        config._config_path = config_path
        config._source_revision = config_revision(content)
        return config

    @classmethod
    def load_saved(cls, path: str | Path) -> "GatewayInstanceConfig":
        """The saved instance configuration, exactly as stored, for a process started from ``path``.

        Owner's decision (2026-09-29): one saved settings file,
        ``<instance_root>/instance_config.yaml`` (``InstanceLayout.instance_config_path``).
        Every settings writer (TeamLead's runtime settings and specialist plans,
        the gateway's configuration editor and manager route) loads it with this
        method, changes only its own fields and saves it back. A process started
        from another file (for example ``TEAM_INSTANCE_CONFIG`` pointing
        elsewhere) still reads and writes the saved file when one exists.
        Processes that run from it use ``load_for_process``.
        """

        loaded = cls.load(path)
        saved = loaded.layout.instance_config_path
        if saved.is_file() and saved != loaded.config_path:
            return cls.load(saved)
        return loaded

    @classmethod
    def load_for_process(
        cls, path: str | Path, *, env: Optional[Mapping[str, str]] = None
    ) -> "GatewayInstanceConfig":
        """The saved configuration with this deployment's details applied in memory.

        Every running process and every in-process reader loads its settings
        here: the saved file (``load_saved``) plus what the deployment supplies
        through the environment (``assistant_gateway.deployment_environment``),
        for example the Docker bundle's own NATS server. Never save the result;
        writers use ``load_saved``.
        """

        from assistant_gateway.deployment_environment import apply_deployment_environment
        from team_transfer.runtime_guard import require_finished_import

        saved = cls.load_saved(path)
        require_finished_import(saved.layout)
        return apply_deployment_environment(saved, os.environ if env is None else env)


def resolve_activity_collector_runtime_base_url(
    config: GatewayInstanceConfig,
) -> str | None:
    """Return the agent-side collector URL implied by an instance config."""
    if config.activity_collector.base_url:
        return config.activity_collector.base_url
    bind_host = (
        str(
            config.activity_collector.bind_host
            or ACTIVITY_COLLECTOR_DEFAULTS.bind_host.value
        ).strip()
        or ACTIVITY_COLLECTOR_DEFAULTS.bind_host.value
    )
    if bind_host in {"0.0.0.0", "::", "[::]"}:
        bind_host = ACTIVITY_COLLECTOR_DEFAULTS.bind_host.value
    if ":" in bind_host and not bind_host.startswith("["):
        bind_host = f"[{bind_host}]"
    return f"http://{bind_host}:{int(config.activity_collector.bind_port)}"
