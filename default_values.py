"""Centralized default values for runtime and onboarding configuration."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Generic, Optional, TypeVar

from custom_messages.contracts.authorization_presentation_mode import (
    AuthorizationPresentationMode,
)
from runtime_settings.model_request_options import LITELLM_API_MODE_CHAT_COMPLETIONS
from runtime_settings.secrets_source import SECRETS_SOURCE_ENV_FILE
from storage.backends import STORAGE_BACKEND_SQLITE
from custom_messages.contracts.runtime_v1 import (
    TEAM_ARCHITECT_AGENT_TYPE,
    TRANSPORT_NATS,
)
from secure_tool_broker.defaults import (
    DEFAULT_BIND_HOST,
    DEFAULT_BIND_PORT,
    DEFAULT_CREDENTIALS_DIR_NAME,
    DEFAULT_CREDENTIAL_STATE_FALLBACK_TO_FILES,
    DEFAULT_CREDENTIAL_STATE_MODE,
    DEFAULT_CREDENTIAL_STATE_NAMESPACE,
    DEFAULT_CREDENTIAL_STATE_SOURCE,
    DEFAULT_INTERNAL_BASE_URL,
    DEFAULT_SEALBOX_API_VERSION,
    DEFAULT_SEALBOX_AUTO_INITIALIZE,
    DEFAULT_SEALBOX_BIND_HOST,
    DEFAULT_SEALBOX_BIND_PORT,
    DEFAULT_SEALBOX_ENABLED,
    DEFAULT_SEALBOX_MANAGE_SERVICE,
    DEFAULT_SEALBOX_READY_TIMEOUT_SECONDS,
    DEFAULT_SEALBOX_ROOT_TOKEN_FILE,
    DEFAULT_SEALBOX_SERVICE_URL,
    DEFAULT_SEALBOX_STORE_PATH,
    DEFAULT_SEALBOX_TENANT_MATERIAL_DIR,
)

T = TypeVar("T")

BUILTIN_OBJECTIVE_EXECUTION_POLICY_URI = "builtin://objective-execution/default"


@dataclass(frozen=True)
class DefaultValue(Generic[T]):
    """A default value plus operator-facing metadata."""

    value: T
    description: str
    onboarding_overridable: bool


def default_value(
    value: T, description: str, *, onboarding_overridable: bool
) -> DefaultValue[T]:
    return DefaultValue(
        value=value,
        description=description,
        onboarding_overridable=onboarding_overridable,
    )


@dataclass(frozen=True)
class TransportDefaultValues:
    type: DefaultValue[str] = default_value(
        TRANSPORT_NATS,
        "Messaging transport backend. NATS/JetStream is the only supported runtime transport.",
        onboarding_overridable=False,
    )
    nats_url: DefaultValue[str] = default_value(
        "nats://127.0.0.1:4222",
        "NATS server URL used by agents, gateways, and operational tools.",
        onboarding_overridable=False,
    )
    nats_servers: DefaultValue[tuple[str, ...]] = default_value(
        ("nats://127.0.0.1:4222",),
        "NATS server URLs used by agents, gateways, and operational tools.",
        onboarding_overridable=False,
    )
    nats_name: DefaultValue[Optional[str]] = default_value(
        None,
        "Optional NATS client name prefix.",
        onboarding_overridable=False,
    )
    jetstream_enabled: DefaultValue[bool] = default_value(
        True,
        "Whether NATS transport should use JetStream for durable work and steering streams.",
        onboarding_overridable=False,
    )
    subject_prefix: DefaultValue[str] = default_value(
        "",
        "Optional team subject prefix for NATS transport subjects. Empty uses the team id.",
        onboarding_overridable=False,
    )
    storage: DefaultValue[str] = default_value(
        "file",
        "JetStream storage policy for runtime streams: file or memory.",
        onboarding_overridable=False,
    )
    replicas: DefaultValue[int] = default_value(
        1,
        "JetStream replica count for runtime streams.",
        onboarding_overridable=False,
    )
    ack_wait_seconds: DefaultValue[float] = default_value(
        3600.0,
        "JetStream acknowledgement wait in seconds for agent work deliveries.",
        onboarding_overridable=False,
    )
    max_deliver: DefaultValue[int] = default_value(
        5,
        "Maximum JetStream delivery attempts before a message is considered poison.",
        onboarding_overridable=False,
    )
    duplicate_window_seconds: DefaultValue[float] = default_value(
        600.0,
        "JetStream duplicate publish detection window in seconds.",
        onboarding_overridable=False,
    )
    work_max_age_seconds: DefaultValue[Optional[float]] = default_value(
        604800.0,
        "Maximum age for durable work/reply messages in JetStream.",
        onboarding_overridable=False,
    )
    presence_max_age_seconds: DefaultValue[Optional[float]] = default_value(
        600.0,
        "Maximum age for replayable presence messages in JetStream.",
        onboarding_overridable=False,
    )
    process_consumer_inactive_threshold_seconds: DefaultValue[float] = default_value(
        300.0,
        "Seconds an unbound process-scoped JetStream consumer remains before NATS deletes it.",
        onboarding_overridable=False,
    )
    nats_user: DefaultValue[Optional[str]] = default_value(
        None,
        "Optional NATS username for non-token auth.",
        onboarding_overridable=False,
    )
    nats_password: DefaultValue[Optional[str]] = default_value(
        None,
        "Optional NATS password or secret-file resolved password for non-token auth.",
        onboarding_overridable=False,
    )
    credentials_file: DefaultValue[Optional[str]] = default_value(
        None,
        "Optional NATS user credentials file path.",
        onboarding_overridable=False,
    )


@dataclass(frozen=True)
class NetworkDefaultValues:
    public_origin: DefaultValue[str] = default_value(
        "http://127.0.0.1",
        "Browser-facing scheme and host shared by local public service URLs.",
        onboarding_overridable=True,
    )


@dataclass(frozen=True)
class LlmDefaultValues:
    provider: DefaultValue[str] = default_value(
        "litellm",
        (
            "Default LLM provider for agents when config, env, and CLI do not override it. "
            "In Universal Agents, litellm selects an OpenAI-compatible endpoint and "
            "litellm_api_mode chooses Chat Completions or Responses; openai selects the "
            "native Responses API."
        ),
        onboarding_overridable=True,
    )
    model_id: DefaultValue[str] = default_value(
        "deepseek/deepseek-v4.1-flash",
        "Default model identifier used for agent reasoning.",
        onboarding_overridable=True,
    )
    temperature: DefaultValue[float] = default_value(
        0.0,
        (
            "Default sampling temperature for providers that support it. OpenAI GPT-5/o-series "
            "models and the OpenAI Codex backend reject this parameter, so those routes omit it."
        ),
        onboarding_overridable=True,
    )
    reasoning_effort: DefaultValue[Optional[str]] = default_value(
        "medium",
        "Default reasoning-effort hint translated to the selected provider transport.",
        onboarding_overridable=False,
    )
    reasoning_summary: DefaultValue[Optional[str]] = default_value(
        None,
        (
            "Optional provider-generated reasoning summary for OpenAI Responses requests: "
            "auto, concise, detailed, or null to disable."
        ),
        onboarding_overridable=False,
    )
    context_window_tokens: DefaultValue[Optional[int]] = default_value(
        800000,
        "Default explicit context-window size used by prompt budgeting and conversation compaction.",
        onboarding_overridable=False,
    )
    max_output_tokens: DefaultValue[Optional[int]] = default_value(
        55296,
        "Default maximum generated tokens for model calls when the provider adapter supports it.",
        onboarding_overridable=False,
    )
    stream: DefaultValue[Optional[bool]] = default_value(
        None,
        (
            "Optional LiteLLM provider streaming override. None keeps the provider adapter "
            "default; Responses mode requires streaming."
        ),
        onboarding_overridable=False,
    )
    litellm_api_mode: DefaultValue[str] = default_value(
        LITELLM_API_MODE_CHAT_COMPLETIONS,
        (
            "Request API used by the litellm provider path: chat_completions for broad "
            "OpenAI-compatible endpoint support, or responses for Responses-capable proxies."
        ),
        onboarding_overridable=False,
    )
    request_timeout_seconds: DefaultValue[float] = default_value(
        120.0,
        (
            "HTTP client timeout for model requests: the longest wait to connect or for "
            "the next bytes of a response. Also bounds a runtime model-switch probe."
        ),
        onboarding_overridable=False,
    )
    stream_idle_timeout_seconds: DefaultValue[float] = default_value(
        300.0,
        (
            "Longest wait for the next output from a model call (text, reasoning, tool "
            "call or finish). Keep-alive pings do not count, so a stalled stream ends."
        ),
        onboarding_overridable=False,
    )
    model_call_timeout_seconds: DefaultValue[float] = default_value(
        1800.0,
        "Longest time one model call may take from request to its last output.",
        onboarding_overridable=False,
    )
    supports_image_input: DefaultValue[Optional[bool]] = default_value(
        None,
        "Whether the selected model/provider accepts image inputs. None means unknown until configured or researched.",
        onboarding_overridable=False,
    )
    supports_audio_input: DefaultValue[Optional[bool]] = default_value(
        None,
        "Whether the selected model/provider accepts audio inputs. None means unknown until configured or researched.",
        onboarding_overridable=False,
    )
    supports_file_input: DefaultValue[Optional[bool]] = default_value(
        None,
        (
            "Whether the selected model/provider accepts native file/document inputs. "
            "None means unknown until configured or researched."
        ),
        onboarding_overridable=False,
    )
    model_capabilities: DefaultValue[dict[str, Any]] = default_value(
        {
            "litellm": {
                "minimax/minimax-m3": {
                    "supports_image_input": True,
                    "supports_audio_input": None,
                }
            }
        },
        "Provider/model-specific model capability facts keyed by provider and model id.",
        onboarding_overridable=False,
    )
    reasoning_trace_retention_messages: DefaultValue[Optional[int]] = default_value(
        96,
        (
            "Number of later Strands messages to keep full provider reasoning traces for tool-call "
            "continuation; null disables pruning."
        ),
        onboarding_overridable=False,
    )
    openai_codex: DefaultValue[dict[str, Any]] = default_value(
        {
            "base_url": "https://chatgpt.com/backend-api/codex",
            "auth_store_path": None,
            "shared_codex_home": None,
            "allow_shared_codex_auth_read": True,
            "refresh_skew_seconds": 120,
            "refresh_timeout_seconds": 20,
            "model_discovery": {
                "enabled": True,
                "cache_path": None,
                "cache_ttl_seconds": 3600,
                "client_version": "1.0.0",
            },
        },
        "OpenAI Codex OAuth provider settings used only when llm.provider is openai-codex.",
        onboarding_overridable=False,
    )
    xai_oauth: DefaultValue[dict[str, Any]] = default_value(
        {
            "base_url": "https://api.x.ai/v1",
            "auth_store_path": None,
            "refresh_skew_seconds": 3600,
            "refresh_timeout_seconds": 20,
        },
        "xAI Grok OAuth provider settings used only when llm.provider is xai-oauth.",
        onboarding_overridable=False,
    )
    compaction_model_provider: DefaultValue[Optional[str]] = default_value(
        None,
        "Optional provider override for conversation compaction calls.",
        onboarding_overridable=False,
    )
    compaction_model_id: DefaultValue[Optional[str]] = default_value(
        None,
        "Optional model override for conversation compaction calls.",
        onboarding_overridable=False,
    )
    litellm_api_base: DefaultValue[Optional[str]] = default_value(
        "https://openrouter.ai/api/v1",
        "Default OpenAI-compatible LiteLLM API base URL.",
        onboarding_overridable=False,
    )
    failover: DefaultValue[dict[str, Any]] = default_value(
        {
            "enabled": False,
            "same_provider_retry": {
                "max_attempts": 2,
                "initial_delay_seconds": 2,
                "max_delay_seconds": 10,
            },
            "switch_for": {
                "min_turns": 3,
                "min_seconds": 900,
            },
            "demotion": {
                "failure_threshold": 3,
                "failure_window_seconds": 3600,
                "demotion_seconds": 86400,
            },
            "history_retention_seconds": 604800,
            "notify_user": True,
            "fallbacks": [],
        },
        "Opt-in model-provider failover policy used when the primary provider is throttled or overloaded.",
        onboarding_overridable=False,
    )


@dataclass(frozen=True)
class WorkspaceDefaultValues:
    instance_root: DefaultValue[str] = default_value(
        ".",
        "Instance root. control/, dbs/, state/ and workspaces/ are derived from it "
        "(runtime_settings.instance_layout).",
        onboarding_overridable=True,
    )
    objective_execution_enabled: DefaultValue[bool] = default_value(
        True,
        "Make bounded Objective execution profiles available for explicit per-Objective activation.",
        onboarding_overridable=False,
    )
    objective_execution_paths: DefaultValue[list[str]] = default_value(
        [BUILTIN_OBJECTIVE_EXECUTION_POLICY_URI],
        "Ordered built-in policy URIs or bounded Objective execution policy paths relative to control/.",
        onboarding_overridable=False,
    )
    skill_installation: DefaultValue[dict[str, Any]] = default_value(
        {
            "enabled": True,
            "scanner_executable": "skillspector",
            "scanner_timeout_seconds": 180,
            "safe_max_score": 20,
            "plan_ttl_seconds": 86400,
            "max_repository_bytes": 104857600,
        },
        "Quarantined GitHub skill acquisition, SkillSpector gate, and immutable plan limits.",
        onboarding_overridable=False,
    )


@dataclass(frozen=True)
class StorageDefaultValues:
    backend: DefaultValue[str] = default_value(
        STORAGE_BACKEND_SQLITE,
        "Default persistence backend for manager, state, and collector stores.",
        onboarding_overridable=True,
    )
    task_history_db_path: DefaultValue[str] = default_value(
        "dbs/task_history.db",
        "Instance-root-relative SQLite database path for task history and shared manager stores.",
        onboarding_overridable=True,
    )
    gateway_state_db_path: DefaultValue[str] = default_value(
        "dbs/gateway_state.db",
        "Instance-root-relative SQLite database path for gateway state.",
        onboarding_overridable=False,
    )
    activity_collector_db_path: DefaultValue[str] = default_value(
        "dbs/activity_collector.db",
        "Instance-root-relative SQLite database path for activity collector events.",
        onboarding_overridable=False,
    )
    sqlite_vector_enabled: DefaultValue[bool] = default_value(
        True,
        "Whether SQLite memory-graph storage should load sqlite-vec and maintain a vector index.",
        onboarding_overridable=False,
    )
    sqlite_vector_provider: DefaultValue[str] = default_value(
        "sqlite_vec",
        "SQLite vector extension provider for memory-graph vector search.",
        onboarding_overridable=False,
    )
    sqlite_vector_require_extension: DefaultValue[bool] = default_value(
        True,
        "Whether SQLite memory-graph storage should fail startup if sqlite-vec cannot load.",
        onboarding_overridable=False,
    )


@dataclass(frozen=True)
class AgentDefaultValues:
    default_team_id: DefaultValue[str] = default_value(
        "team_ua",
        "Default team identifier used for manager launch and onboarding.",
        onboarding_overridable=True,
    )
    default_request_timezone: DefaultValue[str] = default_value(
        "America/Chicago",
        "Default IANA timezone for task requests without an explicit user timezone.",
        onboarding_overridable=True,
    )
    no_team_id: DefaultValue[str] = default_value(
        "NO_TEAM",
        "Internal sentinel team ID for agents constructed without a configured team.",
        onboarding_overridable=False,
    )
    named_agent_type: DefaultValue[str] = default_value(
        TEAM_ARCHITECT_AGENT_TYPE,
        "Default named agent type for the onboarded manager target.",
        onboarding_overridable=True,
    )
    user_queue_postfix: DefaultValue[str] = default_value(
        "user_interact_queue",
        "Queue-name suffix used for user interaction queues.",
        onboarding_overridable=False,
    )
    user_name_postfix: DefaultValue[str] = default_value(
        "user",
        "Name suffix used for user-facing queue identity.",
        onboarding_overridable=False,
    )
    broadcast_history_length: DefaultValue[int] = default_value(
        30,
        "Number of recent presence broadcasts retained in memory.",
        onboarding_overridable=False,
    )
    max_broadcast_age_ms: DefaultValue[int] = default_value(
        100,
        "Maximum accepted age in milliseconds for received presence broadcasts.",
        onboarding_overridable=False,
    )
    delete_fanout_on_exit: DefaultValue[bool] = default_value(
        False,
        "Whether a team lead deletes the fanout exchange on exit.",
        onboarding_overridable=False,
    )
    paused: DefaultValue[bool] = default_value(
        False,
        "Initial paused state for a newly constructed agent.",
        onboarding_overridable=False,
    )
    prompt_skill_description_limit: DefaultValue[int] = default_value(
        50,
        "Maximum deduplicated skill descriptions shown in the AgentSkills system-prompt menu; all skills remain activatable.",
        onboarding_overridable=False,
    )
    memory_graph_enabled: DefaultValue[bool] = default_value(
        False,
        "Whether graph-backed memory infrastructure is enabled.",
        onboarding_overridable=True,
    )
    memory: DefaultValue[dict[str, Any]] = default_value(
        {
            "plugin_manager": {
                "enabled": True,
                "registry_mode": "builtin",
                "fail_open": True,
                "emit_plugin_warnings": True,
            },
            "plugins": {
                "order": [
                    "coremem",
                    "usermem",
                    "domain_topic_recall",
                    "knowledge_recall",
                ],
                "coremem": {
                    "enabled": True,
                    "priority": 34,
                    "pinned": True,
                    "degradable": False,
                },
                "usermem": {
                    "enabled": True,
                    "priority": 35,
                    "pinned": True,
                    "degradable": False,
                },
                "domain_topic_recall": {
                    "enabled": True,
                    "priority": 44,
                    "pinned": False,
                    "degradable": True,
                },
                "knowledge_recall": {
                    "enabled": True,
                    "priority": 45,
                    "pinned": False,
                    "degradable": True,
                    "max_pages": 5,
                },
            },
            "coremem": {
                "enabled": True,
                "max_facts_per_team": 200,
                "max_metadata_chars": 2000,
                "max_tags": 20,
                "max_tag_chars": 64,
                "hydrate_into_context": True,
                "automatic_creation_enabled": True,
                "agent_write_tools_enabled": True,
            },
            "usermem": {
                "enabled": True,
                "max_facts_per_user": 500,
                "require_canonical_user_id": True,
                "max_metadata_chars": 2000,
                "max_tags": 20,
                "max_tag_chars": 64,
                "hydrate_into_context": True,
                "automatic_creation_enabled": True,
                "agent_write_tools_enabled": True,
            },
            "review": {
                "enabled": True,
                "mode": "cue_plus_periodic",
                "periodic_turn_interval": 15,
                "sliding_window_overlap_turns": 5,
                "explicit_cue_immediate": True,
                "max_turn_text_chars": 4000,
                "stale_conversation_after_seconds": 86400,
                "max_stale_conversations_per_run": 20,
                "stale_conversation_review_interval_seconds": 3600,
                "coremem_audit_interval_seconds": 86400,
                "usermem_audit_interval_seconds": 86400,
                "claim_ttl_seconds": 300,
                "memreview_agent_count": 2,
                "review_task_ttl_seconds": 14400,
            },
            "notifications": {
                "quiet_hours_aware": True,
                "active_engagement_grace_seconds": 300,
                "delivery_target": "default",
            },
            "domain": {
                "enabled": True,
                "auto_tag_threads": True,
                "visible_notices": True,
                "max_recall_threads": 5,
            },
        },
        "Memory plugin manager, pinned memory, review, and domain-topic memory settings.",
        onboarding_overridable=False,
    )
    schedule_workers: DefaultValue[dict[str, Any]] = default_value(
        {
            "enabled": True,
            "eligible_agent_types": ["WebResearchAgent", "PersonalAdminAgent"],
            "default_startup_policy_enabled": True,
            "denied_agent_types": [],
            "reconcile_interval_seconds": 5.0,
            "max_claim_wait_seconds": 86400.0,
            "max_startup_timeout_seconds": 3600.0,
            "max_admission_timeout_seconds": 3600.0,
            "max_launch_attempts_per_occurrence": 2,
            "max_idle_ttl_seconds": 86400.0,
            "lease_seconds": 120.0,
            "lease_loss_wait_timeout_seconds": 1800.0,
            "max_consecutive_interruptions": 3,
            "review_interval_seconds": 3600.0,
            "missed_one_time_after_seconds": 3600.0,
            "retention_days": 30,
            "defaults": {
                "claim_wait_seconds": 30.0,
                "startup_timeout_seconds": 120.0,
                "admission_timeout_seconds": 60.0,
                "max_launch_attempts_per_occurrence": 2,
                "retry_backoff_seconds": 30.0,
                "launch_when_busy": False,
                "idle_ttl_seconds": None,
                "interrupted_run_skip_window_seconds": 900.0,
            },
        },
        "Bounded automatic startup with TeamLead-managed registered candidates; explicit operator denies and launch limits remain authoritative.",
        onboarding_overridable=False,
    )
    worker_pools: DefaultValue[dict[str, Any]] = default_value(
        {
            "enabled": True,
            "default_max_workers_per_type": 4,
            "max_workers_per_type_ceiling": 16,
            "max_total_workers_per_team": 32,
            "max_launches_per_type_per_hour": 60,
            "scale_up_wait_seconds": 20.0,
            "max_starts_per_pass": 4,
            "capacity_warning_ratio": 0.95,
            "review_repeat_after_minutes": 30.0,
            "recurring_incident_count": 3,
            "recurring_incident_window_hours": 24.0,
            "max_retries_after_worker_loss": 3,
            "provider_limit": {
                "cooldown_seconds": 60.0,
                "max_cooldown_seconds": 600.0,
                "max_retries": 3,
                "review_events": 3,
                "review_window_minutes": 10.0,
            },
            "progress_notices": {
                "min_tasks": 3,
                "interval_minutes": 5.0,
            },
        },
        "Worker pools: trusted code starts workers for waiting delegated tasks within operator ceilings; TeamArchitect manages pool sizes inside them.",
        onboarding_overridable=False,
    )
    schedule_maintenance: DefaultValue[dict[str, Any]] = default_value(
        {
            "internal_improvement_review_interval_seconds": 21600,
            "memory_review_cleanup": {
                "enabled": True,
                "scan_interval_seconds": 300,
                "expired_claim_grace_seconds": 3600,
                "batch_size": 100,
                "archive_retention_days": 90,
            },
            "stale_schedules": {
                "enabled": True,
                "turned_off_days": 30,
                "paused_beyond_days": 90,
            },
            "stuck_tasks": {
                "enabled": True,
                "owner_gone_after_seconds": 600,
                "waiting_on_person_after_seconds": 86400,
                "no_progress_after_seconds": 7200,
                "handback_unclaimed_after_seconds": 1800,
            },
        },
        "Cadence for the singleton manager-owned InternalImprovement maintenance schedule; non-positive disables installation.",
        onboarding_overridable=False,
    )
    max_queued_top_level_tasks: DefaultValue[int] = default_value(
        50,
        "Maximum queued top-level tasks retained before new submissions are rejected.",
        onboarding_overridable=False,
    )
    max_concurrent_top_level_tasks: DefaultValue[int] = default_value(
        1,
        "Number of top-level tasks the local agent may execute concurrently.",
        onboarding_overridable=False,
    )
    task_queue_warning_threshold: DefaultValue[int] = default_value(
        5,
        "Queued-task count that triggers queue pressure warnings.",
        onboarding_overridable=False,
    )
    subtask_timeout_seconds: DefaultValue[Optional[float]] = default_value(
        1200.0,
        "Maximum wait for delegated subtasks and task-status queries before local failure.",
        onboarding_overridable=False,
    )
    subtask_watchdog_interval_seconds: DefaultValue[float] = default_value(
        15.0,
        "Polling interval for delegated-subtask timeout recovery.",
        onboarding_overridable=False,
    )
    subtask_status_probe_timeout_seconds: DefaultValue[float] = default_value(
        5.0,
        "Maximum wait for a task-status probe during overdue subtask recovery.",
        onboarding_overridable=False,
    )
    restart_executing_task_grace_seconds: DefaultValue[Optional[float]] = default_value(
        900.0,
        "Maximum age for interrupted executing runtime snapshots to be retried after restart.",
        onboarding_overridable=False,
    )
    same_role_runtime_takeover_enabled: DefaultValue[bool] = default_value(
        True,
        "Whether same-role replacement agents may claim persisted active/deferred runtime state.",
        onboarding_overridable=False,
    )
    max_tool_calls_per_task: DefaultValue[Optional[int]] = default_value(
        150,
        "Cumulative tool calls allowed across one ordinary local task-processing invocation.",
        onboarding_overridable=False,
    )
    max_tool_result_tokens_per_call: DefaultValue[Optional[int]] = default_value(
        20000,
        "Maximum estimated tokens allowed in one tool result before offloading it to task-aware artifacts.",
        onboarding_overridable=False,
    )
    runtime_budget_policy: DefaultValue[dict[str, Any]] = default_value(
        {
            "mode": "user_guided",
            "partial_tool_call_budget_multiplier": 1.5,
            "max_user_guided_pauses_per_task": 3,
            "triggers": {
                "tool_call_budget_exceeded": True,
                "context_budget_exceeded": True,
                "delegated_subtask_timeout": True,
                "specialist_runtime_budget_pause": True,
                "provider_overloaded": True,
            },
        },
        "Recoverable runtime budget policy for hard-stop or user-guided pause behavior.",
        onboarding_overridable=False,
    )
    turn_action_outbox: DefaultValue[dict[str, Any]] = default_value(
        {
            "max_auto_attempts": 4,
            "initial_retry_delay_seconds": 2.0,
            "max_retry_delay_seconds": 60.0,
            "retry_jitter_ratio": 0.15,
            "max_inline_recovery_attempts_per_batch": 2,
            "max_validation_repair_attempts": 1,
            "dispatching_stale_after_seconds": 30.0,
        },
        "Runtime action outbox retry and recovery policy.",
        onboarding_overridable=False,
    )
    runtime_coordinator: DefaultValue[dict[str, Any]] = default_value(
        {
            "enabled": True,
            "mode": "enforced",
            "batch_lease_stale_after_seconds": 60.0,
            "max_deferred_events_per_task": 500,
            "max_mailbox_drain_events_per_pass": 100,
            "mailbox_event_retention_seconds": 86400.0,
            "check_interrupts_between_actions": True,
            "defer_scheduled_events_during_active_batch": True,
            "defer_task_responses_during_active_batch": True,
            "defer_recovery_events_during_active_batch": True,
        },
        "Runtime coordinator policy for task-tree leases, mailbox deferral, and state projection.",
        onboarding_overridable=False,
    )
    mid_turn_steering: DefaultValue[dict[str, Any]] = default_value(
        {
            "enabled": True,
            "proactive_guidance_ttl_seconds": 1800.0,
            "interrupt_response_timeout_seconds": 86400.0,
            "allow_restart_fallback": True,
            "apply_proactive_guidance_before_tools": True,
            "apply_proactive_guidance_after_model": True,
        },
        "Runtime policy for Strands mid-turn interrupts and proactive requester steering.",
        onboarding_overridable=False,
    )
    inner_loop_context: DefaultValue[dict[str, Any]] = default_value(
        {
            "enabled": True,
            "max_total_tokens": 300000,
            "warning_ratio": 0.75,
            "aggressive_ratio": 0.90,
            "preserve_recent_messages": 8,
            "preserve_head_tokens": 1000,
            "preserve_tail_tokens": 1000,
            "projected_token_pressure": {
                "enabled": True,
                "threshold_ratio": 0.85,
                "target_ratio": 0.75,
                "fallback_context_window_tokens": None,
            },
            "reactive_overflow": {
                "max_total_recovery_attempts": 3,
                "min_reduction_tokens": 1,
            },
            "filters": {
                "html_document": {"enabled": True, "trigger_tokens": 2000},
                "javascript_or_bundle": {"enabled": True, "trigger_tokens": 1000},
                "css_or_style_blob": {"enabled": True, "trigger_tokens": 2000},
                "base64_or_binary_like": {"enabled": True, "trigger_tokens": 500},
                "minified_or_low_entropy_noise": {
                    "enabled": True,
                    "trigger_tokens": 750,
                },
                "json_large": {
                    "enabled": True,
                    "trigger_tokens": 3000,
                    "comms_json_lenient": True,
                    "comms_json_trigger_tokens": 20000,
                },
                "xml_or_svg": {"enabled": True, "trigger_tokens": 2000},
                "log_or_trace": {"enabled": True, "trigger_tokens": 750},
                "csv_or_table": {"enabled": True, "trigger_tokens": 2500},
                "html_boilerplate_text": {"enabled": True, "trigger_tokens": 1000},
                "agent_transcript": {"enabled": True, "trigger_tokens": 1500},
                "code_file": {"enabled": True, "trigger_tokens": 30000},
            },
        },
        "Inner-loop Strands transcript filtering and token-budget settings.",
        onboarding_overridable=False,
    )
    context_engine: DefaultValue[dict[str, Any]] = default_value(
        {
            "type": "default",
            "max_context_ratio": 0.85,
            "reserved_output_tokens": 55296,
            "hard_limit_behavior": "recover_then_error",
            "overflow_recovery": {
                "enabled": True,
                "max_preflight_attempts": 2,
                "max_provider_retry_attempts": 1,
                "degrade_sections": [
                    "task_runtime_timing",
                    "domain_topic_recall",
                    "knowledge_recall",
                    "task_history",
                    "workspace_user_context",
                    "instance_delivery_context",
                ],
            },
            "context_epochs": {
                "enabled": True,
                "auto_create": True,
                "summary_max_tokens": 5000,
                "include_source_accounting": True,
            },
            "strands_state_snapshots": {
                "enabled": "auto",
                "persist": False,
            },
            "plugins": [],
            "thread_window": {
                "recent_full_turns": 4,
                "per_turn_summary_max_tokens": 400,
                "max_turns_to_load": 100,
                "omitted_notice_enabled": True,
                "include_turn_ids": True,
                "retrieval_tool_hint": True,
            },
            "task_runtime_timing": {
                "enabled": True,
                "include_message_created_at": True,
                "include_handled_at": True,
                "include_processing_started_at": True,
                "include_processing_finished_at": False,
            },
        },
        "Outer-loop context assembly, budget enforcement, and conversation-thread rendering settings.",
        onboarding_overridable=False,
    )
    debug: DefaultValue[dict[str, Any]] = default_value(
        {
            "context_dump": {
                "enabled": False,
                "mode": "on_error",
                "include_system_prompt": True,
                "include_full_prompt": True,
                "include_context_sections": True,
                "include_source_lengths": True,
                "include_incoming_envelope_json": False,
                "include_strands_messages": True,
                "include_tool_results": True,
                "include_result": True,
                "include_reasoning_artifacts": True,
                "capture_model_calls": False,
                "include_model_call_inputs": False,
                "include_tool_specs": False,
                "redact_secrets": True,
                "retention_days": 14,
                "web_chat_controls_enabled": False,
            },
            "reply_routing_trace": {
                "enabled": True,
            },
            "terminal_reasoning": {
                "enabled": False,
            },
        },
        "Opt-in debugging artifacts for model-visible context and observable runtime traces.",
        onboarding_overridable=False,
    )
    structured_output_failure_threshold: DefaultValue[Optional[int]] = default_value(
        4,
        "Optional threshold for stopping repeated structured-output validation failures.",
        onboarding_overridable=False,
    )
    conversation_summary_generator_type: DefaultValue[str] = default_value(
        "heuristic",
        "Strategy type used for durable per-turn conversation summaries. Use llm_summarizing to opt into model-generated summaries.",
        onboarding_overridable=False,
    )
    deferred_message_warning_threshold: DefaultValue[int] = default_value(
        10,
        "Deferred inbound backlog size that triggers runtime warnings.",
        onboarding_overridable=False,
    )
    max_deferred_messages: DefaultValue[int] = default_value(
        100,
        "Maximum durable deferred messages admitted by one agent before broker redelivery.",
        onboarding_overridable=False,
    )


@dataclass(frozen=True)
class EmbeddingDefaultValues:
    provider: DefaultValue[str] = default_value(
        "openai_compatible",
        "Default embedding provider used when memory graph retrieval is not service-backed.",
        onboarding_overridable=True,
    )
    model_id: DefaultValue[Optional[str]] = default_value(
        "harrier-oss-v1-0.6b",
        "Default embedding model identifier.",
        onboarding_overridable=True,
    )
    api_base: DefaultValue[Optional[str]] = default_value(
        None,
        "Optional OpenAI-compatible embedding API base URL.",
        onboarding_overridable=False,
    )
    timeout_seconds: DefaultValue[float] = default_value(
        10.0,
        "HTTP timeout for service-backed embedding requests.",
        onboarding_overridable=False,
    )
    organization: DefaultValue[str] = default_value(
        "",
        "Optional organization identifier for embedding requests.",
        onboarding_overridable=False,
    )
    extra_headers: DefaultValue[dict[str, str]] = default_value(
        {},
        "Additional non-secret headers for embedding requests.",
        onboarding_overridable=False,
    )


@dataclass(frozen=True)
class ConversationCompactionDefaultValues:
    compactor_type: DefaultValue[str] = default_value(
        "heuristic_sliding_window",
        "Default conversation compaction strategy.",
        onboarding_overridable=False,
    )
    trigger_ratio: DefaultValue[float] = default_value(
        0.75,
        "Context-window fraction that triggers compaction.",
        onboarding_overridable=False,
    )
    target_ratio: DefaultValue[float] = default_value(
        0.50,
        "Context-window fraction targeted after compaction.",
        onboarding_overridable=False,
    )
    min_recent_turns: DefaultValue[int] = default_value(
        3,
        "Minimum raw conversation turns preserved during compaction.",
        onboarding_overridable=False,
    )
    max_recent_turns: DefaultValue[int] = default_value(
        8,
        "Maximum raw recent turns preserved when compaction is active.",
        onboarding_overridable=False,
    )


@dataclass(frozen=True)
class GatewayDefaultValues:
    instance_root_name: DefaultValue[str] = default_value(
        ".team_nats_agents/instances",
        "Home-directory-relative root for gateway instances when no root is supplied.",
        onboarding_overridable=True,
    )
    assistant_display_name: DefaultValue[str] = default_value(
        "Lisa",
        "Default assistant display name written to instance configuration.",
        onboarding_overridable=True,
    )
    user_display_name: DefaultValue[str] = default_value(
        "User",
        "Default user display name written to instance configuration.",
        onboarding_overridable=True,
    )
    user_timezone: DefaultValue[str] = default_value(
        "America/Chicago",
        "Default IANA timezone for user-facing scheduling and delivery.",
        onboarding_overridable=True,
    )
    default_session_key: DefaultValue[str] = default_value(
        "default",
        "Default conversation/session key for user-facing gateway interactions.",
        onboarding_overridable=False,
    )
    delivery_channel_name: DefaultValue[str] = default_value(
        "telegram",
        "Default delivery target channel used when no adapter-specific channel is configured.",
        onboarding_overridable=False,
    )
    manager_request_timeout_seconds: DefaultValue[float] = default_value(
        3600.0,
        "Maximum wait for a manager response to a gateway request.",
        onboarding_overridable=True,
    )
    users_enabled: DefaultValue[bool] = default_value(
        True,
        "Whether the gateway resolves durable canonical users from channel bindings.",
        onboarding_overridable=False,
    )
    users_allow_newuser_registration: DefaultValue[bool] = default_value(
        True,
        "Whether admins can register canonical gateway identities with /user.",
        onboarding_overridable=False,
    )
    users_link_code_ttl_seconds: DefaultValue[int] = default_value(
        900,
        "Lifetime in seconds for /user link codes.",
        onboarding_overridable=False,
    )
    users_web_user_invite_ttl_seconds: DefaultValue[int] = default_value(
        900,
        "Lifetime in seconds for one-time Web Chat invitation codes.",
        onboarding_overridable=False,
    )
    users_web_user_tokens_enabled: DefaultValue[bool] = default_value(
        True,
        "Whether registered canonical users can authenticate to Web Chat with user-scoped tokens.",
        onboarding_overridable=False,
    )
    users_web_user_token_ttl_days: DefaultValue[int] = default_value(
        0,
        "Lifetime in days for generated Web Chat user tokens; 0 disables automatic expiration.",
        onboarding_overridable=False,
    )
    users_removed_data_retention_days: DefaultValue[int] = default_value(
        7,
        "Days a removed person's folder, personal memory, own knowledge pages and conversations are held so /user restore can bring them back; a maintenance run then deletes them.",
        onboarding_overridable=False,
    )
    manager_transition_notices_enabled: DefaultValue[bool] = default_value(
        False,
        "Whether the gateway emits proactive manager outage and recovery messages.",
        onboarding_overridable=False,
    )
    unavailable_message_template: DefaultValue[str] = default_value(
        "{assistant_name} is not currently available. Please try again after it reconnects.",
        "Gateway message sent when the assistant manager is offline.",
        onboarding_overridable=False,
    )
    available_message_template: DefaultValue[str] = default_value(
        "{assistant_name} is now available.",
        "Gateway message sent when the assistant manager becomes available.",
        onboarding_overridable=False,
    )


@dataclass(frozen=True)
class DeliveryDefaultValues:
    quiet_hours_enabled: DefaultValue[bool] = default_value(
        True,
        "Whether deferred non-urgent delivery is enabled during quiet hours.",
        onboarding_overridable=True,
    )
    quiet_start_hour_local: DefaultValue[int] = default_value(
        22,
        "Local hour when quiet hours begin.",
        onboarding_overridable=True,
    )
    quiet_end_hour_local: DefaultValue[int] = default_value(
        8,
        "Local hour when quiet hours end.",
        onboarding_overridable=True,
    )
    allow_urgent_delivery: DefaultValue[bool] = default_value(
        True,
        "Whether urgent messages bypass quiet-hour deferral.",
        onboarding_overridable=False,
    )
    digest_delivery_hour_local: DefaultValue[int] = default_value(
        8,
        "Local hour when deferred-message digests are delivered.",
        onboarding_overridable=True,
    )
    summarize_deferred: DefaultValue[bool] = default_value(
        True,
        "Whether deferred messages are summarized before digest delivery.",
        onboarding_overridable=False,
    )
    notification_routing_mode: DefaultValue[str] = default_value(
        "source_thread_or_default",
        "How notify_user target='auto' routes agent notifications: source_thread_or_default or default_target.",
        onboarding_overridable=False,
    )
    urgent_keywords: DefaultValue[tuple[str, ...]] = default_value(
        ("urgent", "asap", "immediately", "critical", "failure", "error"),
        "Keywords that mark inbound messages as urgent for delivery policy.",
        onboarding_overridable=False,
    )


@dataclass(frozen=True)
class HeartbeatDefaultValues:
    enabled: DefaultValue[bool] = default_value(
        True,
        "Whether gateway-created heartbeat schedules are enabled by default.",
        onboarding_overridable=True,
    )
    interval_seconds: DefaultValue[int] = default_value(
        1800,
        "Default interval between heartbeat prompts.",
        onboarding_overridable=True,
    )
    session_key: DefaultValue[str] = default_value(
        "heartbeat",
        "Default session key used for heartbeat messages.",
        onboarding_overridable=False,
    )
    active_hours_enabled: DefaultValue[bool] = default_value(
        False,
        "Whether heartbeat active-hour filtering is enabled by default.",
        onboarding_overridable=False,
    )
    active_start_hour_local: DefaultValue[int] = default_value(
        8,
        "Local hour when heartbeat active hours begin.",
        onboarding_overridable=False,
    )
    active_end_hour_local: DefaultValue[int] = default_value(
        18,
        "Local hour when heartbeat active hours end.",
        onboarding_overridable=False,
    )


@dataclass(frozen=True)
class InteractiveAppDefaultValues:
    internal_website_enabled: DefaultValue[bool] = default_value(
        True,
        "Whether the internal docs/generated-site website host starts with gateway interactive apps.",
        onboarding_overridable=False,
    )
    internal_website_bind_host: DefaultValue[str] = default_value(
        "0.0.0.0",
        "Internal docs/generated-site website host bind interface.",
        onboarding_overridable=False,
    )
    internal_website_bind_port: DefaultValue[int] = default_value(
        18090,
        "Internal docs/generated-site website host bind port.",
        onboarding_overridable=False,
    )
    internal_website_public_port: DefaultValue[Optional[int]] = default_value(
        18090,
        "Browser-facing internal website port combined with network.public_origin.",
        onboarding_overridable=False,
    )
    internal_website_docs_root: DefaultValue[Optional[str]] = default_value(
        None,
        "Optional filesystem root for static user-facing documentation pages; null uses repository user_docs/site.",
        onboarding_overridable=False,
    )
    internal_website_skills_dir: DefaultValue[Optional[str]] = default_value(
        None,
        "Optional skills directory override used when generating skill docs.",
        onboarding_overridable=False,
    )
    internal_website_web_chat_enabled: DefaultValue[bool] = default_value(
        True,
        "Whether the internal website host serves the built-in Web Chat interface.",
        onboarding_overridable=True,
    )
    internal_website_web_chat_token_env_var: DefaultValue[str] = default_value(
        "WEB_CHAT_AUTH_TOKEN",
        "Environment variable containing the built-in Web Chat authentication token.",
        onboarding_overridable=False,
    )
    internal_website_attachment_enabled: DefaultValue[bool] = default_value(
        True,
        "Whether built-in Web Chat accepts and displays attachments.",
        onboarding_overridable=False,
    )
    internal_website_attachment_max_file_bytes: DefaultValue[int] = default_value(
        30 * 1024 * 1024,
        "Maximum Web Chat attachment size in bytes.",
        onboarding_overridable=False,
    )
    internal_website_attachment_allowed_kinds: DefaultValue[tuple[str, ...]] = (
        default_value(
            ("image", "document", "audio", "video"),
            "Attachment kinds accepted by built-in Web Chat.",
            onboarding_overridable=False,
        )
    )
    internal_website_attachment_nats_object_bucket: DefaultValue[str] = default_value(
        "ua_attachments",
        "NATS Object Store bucket for on-demand remote attachment copies.",
        onboarding_overridable=False,
    )
    internal_website_attachment_nats_object_ttl_seconds: DefaultValue[int] = (
        default_value(
            86400,
            "NATS Object Store retention for on-demand attachment copies.",
            onboarding_overridable=False,
        )
    )
    internal_website_attachment_nats_object_max_bytes: DefaultValue[Optional[int]] = (
        default_value(
            None,
            "Optional maximum NATS Object Store bytes for remote attachment copies.",
            onboarding_overridable=False,
        )
    )
    internal_website_attachment_remote_copy_enabled: DefaultValue[bool] = default_value(
        True,
        "Whether the gateway can mirror Web Chat attachments to NATS Object Store on request.",
        onboarding_overridable=False,
    )
    group_policy: DefaultValue[str] = default_value(
        "mention",
        "Default group-chat policy for Telegram and Discord adapters.",
        onboarding_overridable=False,
    )
    telegram_manager_offline_policy: DefaultValue[str] = default_value(
        "pause_telegram_polling",
        "Telegram behavior when the manager is offline.",
        onboarding_overridable=False,
    )
    reply_to_message: DefaultValue[bool] = default_value(
        True,
        "Whether interactive adapters reply to the source message by default.",
        onboarding_overridable=False,
    )
    scoped_sessions: DefaultValue[bool] = default_value(
        True,
        "Whether adapters scope sessions to topics or threads by default.",
        onboarding_overridable=False,
    )
    drop_pending_updates: DefaultValue[bool] = default_value(
        True,
        "Whether Telegram drops pending updates on startup.",
        onboarding_overridable=False,
    )
    telegram_connection_pool_size: DefaultValue[int] = default_value(
        32,
        "Telegram HTTP connection pool size.",
        onboarding_overridable=False,
    )
    telegram_concurrent_updates: DefaultValue[int] = default_value(
        8,
        "Maximum Telegram updates processed concurrently by the app adapter.",
        onboarding_overridable=False,
    )
    telegram_pool_timeout: DefaultValue[float] = default_value(
        5.0,
        "Telegram connection pool wait timeout in seconds.",
        onboarding_overridable=False,
    )
    whatsapp_webhook_host: DefaultValue[str] = default_value(
        "0.0.0.0",
        "WhatsApp webhook bind host.",
        onboarding_overridable=False,
    )
    whatsapp_webhook_port: DefaultValue[int] = default_value(
        8080,
        "WhatsApp webhook bind port.",
        onboarding_overridable=False,
    )
    whatsapp_webhook_path: DefaultValue[str] = default_value(
        "/webhooks/whatsapp",
        "WhatsApp webhook URL path.",
        onboarding_overridable=False,
    )
    validate_twilio_signature: DefaultValue[bool] = default_value(
        True,
        "Whether WhatsApp webhook requests require Twilio signature validation.",
        onboarding_overridable=False,
    )
    signal_base_url: DefaultValue[str] = default_value(
        "http://127.0.0.1:8080",
        "Base URL for the authenticated signal-cli-rest-api server.",
        onboarding_overridable=True,
    )
    signal_group_policy: DefaultValue[str] = default_value(
        "mention",
        "Signal group handling policy: mention or open.",
        onboarding_overridable=True,
    )
    signal_poll_timeout_seconds: DefaultValue[float] = default_value(
        5.0,
        "Signal receive long-poll timeout in seconds.",
        onboarding_overridable=False,
    )
    signal_poll_interval_seconds: DefaultValue[float] = default_value(
        0.25,
        "Delay between Signal receive polls after a successful empty response.",
        onboarding_overridable=False,
    )
    signal_max_messages_per_poll: DefaultValue[int] = default_value(
        10,
        "Maximum Signal messages requested per receive poll.",
        onboarding_overridable=False,
    )
    signal_request_timeout_seconds: DefaultValue[float] = default_value(
        15.0,
        "Signal REST request timeout in seconds.",
        onboarding_overridable=False,
    )
    signal_ignore_attachments: DefaultValue[bool] = default_value(
        False,
        "Whether Signal receive polling asks the REST API to ignore attachment downloads.",
        onboarding_overridable=False,
    )
    signal_ignore_stories: DefaultValue[bool] = default_value(
        True,
        "Whether Signal receive polling ignores stories.",
        onboarding_overridable=False,
    )
    signal_ignore_avatars: DefaultValue[bool] = default_value(
        True,
        "Whether Signal receive polling ignores avatars.",
        onboarding_overridable=False,
    )
    signal_ignore_stickers: DefaultValue[bool] = default_value(
        True,
        "Whether Signal receive polling ignores stickers.",
        onboarding_overridable=False,
    )
    signal_send_read_receipts: DefaultValue[bool] = default_value(
        False,
        "Whether Signal receive polling sends read receipts.",
        onboarding_overridable=False,
    )
    signal_drop_pending_messages_on_startup: DefaultValue[bool] = default_value(
        False,
        "Whether Signal drops already queued receive messages when the adapter starts.",
        onboarding_overridable=False,
    )
    signal_text_mode: DefaultValue[str] = default_value(
        "normal",
        "Signal outbound text mode passed to /v2/send.",
        onboarding_overridable=False,
    )
    photon_project_id_env_var: DefaultValue[str] = default_value(
        "PHOTON_PROJECT_ID",
        "Environment variable containing the Photon Spectrum project id.",
        onboarding_overridable=False,
    )
    photon_project_secret_env_var: DefaultValue[str] = default_value(
        "PHOTON_PROJECT_SECRET",
        "Environment variable containing the Photon Spectrum project secret.",
        onboarding_overridable=False,
    )
    photon_sidecar_token_env_var: DefaultValue[str] = default_value(
        "PHOTON_SIDECAR_TOKEN",
        "Optional environment variable containing a pre-shared Photon sidecar token.",
        onboarding_overridable=False,
    )
    photon_sidecar_bind_host: DefaultValue[str] = default_value(
        "127.0.0.1",
        "Loopback bind host for the Photon sidecar control server.",
        onboarding_overridable=False,
    )
    photon_sidecar_port: DefaultValue[int] = default_value(
        8789,
        "Loopback port for the Photon sidecar control server.",
        onboarding_overridable=True,
    )
    photon_sidecar_autostart: DefaultValue[bool] = default_value(
        True,
        "Whether the Photon adapter starts the bundled Node sidecar.",
        onboarding_overridable=False,
    )
    photon_node_bin: DefaultValue[str] = default_value(
        "node",
        "Node.js executable used to launch the Photon sidecar.",
        onboarding_overridable=False,
    )
    photon_group_policy: DefaultValue[str] = default_value(
        "mention",
        "Photon group handling policy: mention or open.",
        onboarding_overridable=True,
    )
    photon_request_timeout_seconds: DefaultValue[float] = default_value(
        15.0,
        "Photon sidecar loopback HTTP request timeout in seconds.",
        onboarding_overridable=False,
    )
    photon_max_inline_attachment_bytes: DefaultValue[int] = default_value(
        20 * 1024 * 1024,
        "Maximum Photon inbound attachment bytes that the sidecar may inline.",
        onboarding_overridable=False,
    )
    photon_markdown: DefaultValue[bool] = default_value(
        True,
        "Whether Photon outbound text is sent to Spectrum as markdown.",
        onboarding_overridable=False,
    )
    photon_telemetry: DefaultValue[bool] = default_value(
        False,
        "Whether the Photon sidecar enables Spectrum telemetry.",
        onboarding_overridable=False,
    )


@dataclass(frozen=True)
class ToolPolicyDefaultValues:
    """Defaults for tool_policy (data folders moved into the access store, decision D16)."""

    scratch_retention_days: DefaultValue[int] = default_value(
        14,
        "Days a file stays in a scratch/ folder (people's and agents') before maintenance deletes it; 0 keeps them.",
        onboarding_overridable=False,
    )


@dataclass(frozen=True)
class VoiceDefaultValues:
    enabled: DefaultValue[bool] = default_value(
        False,
        "Whether voice input/output is enabled for gateway adapters.",
        onboarding_overridable=False,
    )
    input_enabled: DefaultValue[bool] = default_value(
        True,
        "Whether speech-to-text input is enabled when voice is active.",
        onboarding_overridable=False,
    )
    output_enabled: DefaultValue[bool] = default_value(
        False,
        "Whether text-to-speech output is enabled when voice is active.",
        onboarding_overridable=False,
    )
    response_mode: DefaultValue[str] = default_value(
        "text_only",
        "Preferred voice reply mode: voice_only, voice_and_text, or text_only.",
        onboarding_overridable=False,
    )
    provider: DefaultValue[str] = default_value(
        "grok",
        "Voice provider used for speech-to-text and text-to-speech.",
        onboarding_overridable=False,
    )
    api_base: DefaultValue[str] = default_value(
        "https://api.x.ai/v1",
        "Base API URL for the default voice provider.",
        onboarding_overridable=False,
    )
    stt_language: DefaultValue[Optional[str]] = default_value(
        "en",
        "Default speech-to-text language hint.",
        onboarding_overridable=False,
    )
    stt_format_text: DefaultValue[bool] = default_value(
        True,
        "Whether speech-to-text output should be text-formatted by the provider.",
        onboarding_overridable=False,
    )
    tts_voice: DefaultValue[str] = default_value(
        "eve",
        "Default text-to-speech voice identifier.",
        onboarding_overridable=False,
    )
    tts_language: DefaultValue[str] = default_value(
        "auto",
        "Default text-to-speech language selection.",
        onboarding_overridable=False,
    )
    tts_output_codec: DefaultValue[str] = default_value(
        "mp3",
        "Default audio codec for text-to-speech output.",
        onboarding_overridable=False,
    )
    tts_sample_rate: DefaultValue[int] = default_value(
        24000,
        "Default sample rate for text-to-speech output.",
        onboarding_overridable=False,
    )
    tts_bit_rate: DefaultValue[Optional[int]] = default_value(
        128000,
        "Default bit rate for text-to-speech output when the codec supports it.",
        onboarding_overridable=False,
    )
    connect_timeout_seconds: DefaultValue[float] = default_value(
        10.0,
        "HTTP connect timeout for voice provider requests.",
        onboarding_overridable=False,
    )
    request_timeout_seconds: DefaultValue[float] = default_value(
        120.0,
        "HTTP request timeout for voice provider calls.",
        onboarding_overridable=False,
    )


@dataclass(frozen=True)
class ActivityCollectorDefaultValues:
    base_url: DefaultValue[Optional[str]] = default_value(
        "http://127.0.0.1:8765",
        "Optional public base URL used by agents to submit activity events.",
        onboarding_overridable=False,
    )
    connect_timeout_seconds: DefaultValue[float] = default_value(
        1.0,
        "HTTP connect timeout for activity collector submissions.",
        onboarding_overridable=False,
    )
    request_timeout_seconds: DefaultValue[float] = default_value(
        2.0,
        "HTTP request timeout for activity collector submissions.",
        onboarding_overridable=False,
    )
    batch_max_events: DefaultValue[int] = default_value(
        25,
        "Maximum number of activity events submitted in one batch.",
        onboarding_overridable=False,
    )
    flush_interval_seconds: DefaultValue[float] = default_value(
        1.0,
        "Periodic flush interval for buffered activity events.",
        onboarding_overridable=False,
    )
    max_buffered_events: DefaultValue[int] = default_value(
        500,
        "Maximum buffered activity events retained before dropping new events.",
        onboarding_overridable=False,
    )
    drop_when_unavailable: DefaultValue[bool] = default_value(
        True,
        "Whether activity events are dropped instead of blocking when the collector is unavailable.",
        onboarding_overridable=False,
    )
    bind_host: DefaultValue[str] = default_value(
        "127.0.0.1",
        "Activity collector HTTP bind host.",
        onboarding_overridable=False,
    )
    bind_port: DefaultValue[int] = default_value(
        8765,
        "Activity collector HTTP bind port.",
        onboarding_overridable=False,
    )
    user_url_path: DefaultValue[str] = default_value(
        "/user",
        "Activity collector user-facing URL path.",
        onboarding_overridable=False,
    )
    admin_url_path: DefaultValue[str] = default_value(
        "/admin",
        "Activity collector admin URL path.",
        onboarding_overridable=False,
    )
    ingest_token_env_var: DefaultValue[str] = default_value(
        "ACTIVITY_COLLECTOR_INGEST_TOKEN",
        "Environment variable containing the activity collector ingest token.",
        onboarding_overridable=False,
    )


@dataclass(frozen=True)
class OtelDefaultValues:
    enabled: DefaultValue[bool] = default_value(
        False,
        "Whether OpenTelemetry export is enabled.",
        onboarding_overridable=False,
    )
    enable_traces: DefaultValue[bool] = default_value(
        False,
        "Whether OTLP trace export is enabled.",
        onboarding_overridable=False,
    )
    enable_metrics: DefaultValue[bool] = default_value(
        False,
        "Whether OTLP metric export is enabled.",
        onboarding_overridable=False,
    )
    otlp_endpoint: DefaultValue[str] = default_value(
        "",
        "Default OTLP collector endpoint; empty disables remote export.",
        onboarding_overridable=False,
    )
    service_name: DefaultValue[str] = default_value(
        "team_nats_agents",
        "Default OpenTelemetry service name.",
        onboarding_overridable=False,
    )
    headers: DefaultValue[dict[str, str]] = default_value(
        {},
        "Additional non-secret OTLP export headers.",
        onboarding_overridable=False,
    )


@dataclass(frozen=True)
class AuthorizationDefaultValues:
    enabled: DefaultValue[bool] = default_value(
        True,
        "Whether every agent tool call and broker request is checked against grants.",
        onboarding_overridable=False,
    )
    cedar_binary_path: DefaultValue[Optional[str]] = default_value(
        None,
        "Optional path or command name of the ua-cedar-authorizer access evaluator.",
        onboarding_overridable=False,
    )
    starter_profile: DefaultValue[str] = default_value(
        "trusted",
        (
            "Access the team lead starts with when authorization is on: trusted (full access "
            "to the standard tool set, may hand it on without asking), careful (read access, "
            "may hand it on), or manual (nothing)."
        ),
        onboarding_overridable=True,
    )
    presentation: DefaultValue[dict[str, Any]] = default_value(
        {"mode": AuthorizationPresentationMode.FRIENDLY.value},
        "How much detail access refusals and review messages show.",
        onboarding_overridable=False,
    )


@dataclass(frozen=True)
class SecurityLayerDefaultValues:
    tool_setup_recheck_days: DefaultValue[int] = default_value(
        30,
        "Days after which the broker re-checks a person's tool that is ready (signed in or linked) but "
        "was not used or checked since; 0 turns the re-check off.",
        onboarding_overridable=False,
    )
    mode: DefaultValue[str] = default_value(
        "broker",
        "Security mode: broker, or openshell to also run agent commands in an OpenShell sandbox. The secure tool broker always runs.",
        onboarding_overridable=True,
    )
    broker_internal_base_url: DefaultValue[Optional[str]] = default_value(
        DEFAULT_INTERNAL_BASE_URL,
        "Agent/gateway-facing base URL for secure tool broker API calls.",
        onboarding_overridable=True,
    )
    broker_public_port: DefaultValue[Optional[int]] = default_value(
        DEFAULT_BIND_PORT,
        "Browser-facing secure tool broker port combined with network.public_origin.",
        onboarding_overridable=True,
    )
    broker_bind_host: DefaultValue[Optional[str]] = default_value(
        DEFAULT_BIND_HOST,
        "Network interface the secure tool broker listens on.",
        onboarding_overridable=True,
    )
    broker_bind_port: DefaultValue[Optional[int]] = default_value(
        DEFAULT_BIND_PORT,
        "Network port the secure tool broker listens on.",
        onboarding_overridable=True,
    )
    broker_credentials_dir: DefaultValue[Optional[str]] = default_value(
        DEFAULT_CREDENTIALS_DIR_NAME,
        "Instance-root-relative directory for broker-owned credential CLI state.",
        onboarding_overridable=True,
    )
    credential_state_backend: DefaultValue[dict[str, Any]] = default_value(
        {
            "mode": DEFAULT_CREDENTIAL_STATE_MODE,
            "source": DEFAULT_CREDENTIAL_STATE_SOURCE,
            "namespace": DEFAULT_CREDENTIAL_STATE_NAMESPACE,
            "fallback_to_files": DEFAULT_CREDENTIAL_STATE_FALLBACK_TO_FILES,
        },
        "Broker authentication state persistence through the configured credential source.",
        onboarding_overridable=False,
    )
    bitwarden_server_url: DefaultValue[Optional[str]] = default_value(
        None,
        "Optional Bitwarden or Vaultwarden server URL configured in the broker-owned bw CLI.",
        onboarding_overridable=False,
    )
    sealbox_enabled: DefaultValue[bool] = default_value(
        DEFAULT_SEALBOX_ENABLED,
        "Whether the secure broker configures Sealbox as an available credential source.",
        onboarding_overridable=False,
    )
    sealbox_manage_service: DefaultValue[bool] = default_value(
        DEFAULT_SEALBOX_MANAGE_SERVICE,
        "Whether headless launch starts a broker-owned sealbox-server process.",
        onboarding_overridable=False,
    )
    sealbox_auto_initialize: DefaultValue[bool] = default_value(
        DEFAULT_SEALBOX_AUTO_INITIALIZE,
        "Whether managed Sealbox startup creates its root administration token when missing.",
        onboarding_overridable=False,
    )
    sealbox_bind_host: DefaultValue[str] = default_value(
        DEFAULT_SEALBOX_BIND_HOST,
        "Network interface the managed sealbox-server listens on.",
        onboarding_overridable=False,
    )
    sealbox_bind_port: DefaultValue[int] = default_value(
        DEFAULT_SEALBOX_BIND_PORT,
        "Network port the managed sealbox-server listens on.",
        onboarding_overridable=False,
    )
    sealbox_store_path: DefaultValue[str] = default_value(
        DEFAULT_SEALBOX_STORE_PATH,
        "Broker-credentials-dir-relative SQLite store path for managed Sealbox.",
        onboarding_overridable=False,
    )
    sealbox_service_url: DefaultValue[Optional[str]] = default_value(
        DEFAULT_SEALBOX_SERVICE_URL,
        "Sealbox service URL; defaults to the managed bind address when omitted.",
        onboarding_overridable=False,
    )
    sealbox_root_token_file: DefaultValue[str] = default_value(
        DEFAULT_SEALBOX_ROOT_TOKEN_FILE,
        "Broker-credentials-dir-relative Sealbox root administration token file.",
        onboarding_overridable=False,
    )
    sealbox_tenant_material_dir: DefaultValue[str] = default_value(
        DEFAULT_SEALBOX_TENANT_MATERIAL_DIR,
        "Broker-credentials-dir-relative directory for opaque tenant tokens and key pairs.",
        onboarding_overridable=False,
    )
    sealbox_api_version: DefaultValue[str] = default_value(
        DEFAULT_SEALBOX_API_VERSION,
        "Sealbox tenant data API version used by the broker.",
        onboarding_overridable=False,
    )
    sealbox_ready_timeout_seconds: DefaultValue[int] = default_value(
        DEFAULT_SEALBOX_READY_TIMEOUT_SECONDS,
        "Seconds to wait for managed sealbox-server readiness.",
        onboarding_overridable=False,
    )
    credential_catalog_enabled: DefaultValue[bool] = default_value(
        True,
        "Whether broker-owned secure credential catalog and collection flows are enabled.",
        onboarding_overridable=False,
    )
    credential_catalog_database_path: DefaultValue[str] = default_value(
        "credential_catalog.db",
        "Instance-root-relative owner-scoped SQLite credential catalog path.",
        onboarding_overridable=False,
    )
    credential_template_overrides_path: DefaultValue[str] = default_value(
        "credential_template_overrides.yaml",
        "Instance-root-relative YAML path for user-approved credential template variants.",
        onboarding_overridable=False,
    )
    credential_catalog_default_write_source: DefaultValue[str] = default_value(
        "sealbox",
        "Default credential store used for broker-owned credential writes.",
        onboarding_overridable=False,
    )
    credential_catalog_openshell_binding_ttl_seconds: DefaultValue[int] = default_value(
        900,
        "Default lifetime for per-use OpenShell credential binding leases.",
        onboarding_overridable=False,
    )
    credential_catalog_collection_session_ttl_seconds: DefaultValue[int] = (
        default_value(
            900,
            "Default lifetime for secure broker credential collection form sessions.",
            onboarding_overridable=False,
        )
    )
    credential_catalog_secret_cache_ttl_seconds: DefaultValue[int] = default_value(
        300,
        "In-memory broker cache TTL for resolved catalog secrets used in consecutive broker actions.",
        onboarding_overridable=False,
    )


@dataclass(frozen=True)
class UIDefaultValues:
    onboarding_color_mode: DefaultValue[str] = default_value(
        "dark",
        "Default color mode shared by onboarding and runtime web interfaces.",
        onboarding_overridable=True,
    )
    onboarding_light_colors: DefaultValue[tuple[str, ...]] = default_value(
        ("#082f73", "#ffd60a", "#20f05a", "#ff3030"),
        "Default onboarding preview light colors for the website guide background.",
        onboarding_overridable=True,
    )
    onboarding_light_diffusion: DefaultValue[int] = default_value(
        58,
        "Default onboarding preview light diffusion percentage.",
        onboarding_overridable=True,
    )
    onboarding_polygon_opacity_low: DefaultValue[int] = default_value(
        20,
        "Default low end of the onboarding preview polygon opacity range.",
        onboarding_overridable=True,
    )
    onboarding_polygon_opacity_high: DefaultValue[int] = default_value(
        70,
        "Default high end of the onboarding preview polygon opacity range.",
        onboarding_overridable=True,
    )
    onboarding_polygon_line_color: DefaultValue[str] = default_value(
        "#202426",
        "Default onboarding preview polygon edge color.",
        onboarding_overridable=True,
    )
    onboarding_polygon_line_opacity: DefaultValue[int] = default_value(
        8,
        "Default onboarding preview polygon edge opacity percentage.",
        onboarding_overridable=True,
    )
    onboarding_polygon_seed: DefaultValue[int] = default_value(
        4147,
        "Stable seed for the diffuse light polygon mesh.",
        onboarding_overridable=True,
    )
    onboarding_dynamic_background: DefaultValue[bool] = default_value(
        True,
        "Whether the onboarding preview background animates by default.",
        onboarding_overridable=True,
    )
    onboarding_interface_animation: DefaultValue[bool] = default_value(
        True,
        "Whether Web Chat and onboarding animate interface elements (panels, messages, activity).",
        onboarding_overridable=True,
    )
    onboarding_dark_appearance: DefaultValue[dict[str, Any]] = default_value(
        {
            "detail": "full",
            "surface_color": "#007007",
            "metal_color": "#015b1a",
            "facet_size": 230,
            "facet_depth": 80,
            "chrome_faces": 0,
            "shine": 51,
            "edge_highlights": 31,
            "surface_grain": 24,
            "light_x": -7,
            "light_y": -5,
            "light_softness": 37,
            "light_motion": True,
            "light_wander": False,
            "light_wander_distance": 133,
            "light_speed": 110,
            "mesh_motion": True,
            "mesh_speed": 100,
            "seed": 9187,
            "accents": {
                "gold": {"color": "#464020", "share": 10},
                "bronze": {"color": "#00751d", "share": 39},
            },
        },
        "Dark metallic background defaults shared by onboarding and runtime web interfaces.",
        onboarding_overridable=True,
    )


@dataclass(frozen=True)
class ExecutionControlDefaultValues:
    enabled: DefaultValue[bool] = default_value(
        True,
        "Enable immutable deterministic execution control-plane services.",
        onboarding_overridable=False,
    )
    runner: DefaultValue[dict[str, Any]] = default_value(
        {
            "enabled": True,
            "tick_interval_seconds": 2,
            "max_concurrent_jobs": 2,
            "lease_ttl_seconds": 60,
            "service_reconcile_interval_seconds": 10,
        },
        "Fenced execution runner and service reconciler settings.",
        onboarding_overridable=False,
    )
    artifact_store: DefaultValue[dict[str, Any]] = default_value(
        {
            "maximum_artifact_bytes": 268435456,
        },
        "Content-addressed artifact storage bounds.",
        onboarding_overridable=False,
    )
    kubernetes: DefaultValue[dict[str, Any]] = default_value(
        {
            "kubectl_executable": "kubectl",
            "network_policy_enforced": False,
        },
        "Kubernetes adapter requirements.",
        onboarding_overridable=False,
    )
    ssh: DefaultValue[dict[str, Any]] = default_value(
        {
            "enabled": False,
            "ssh_executable": "ssh",
            "lease_ttl_seconds": 300,
            "transcript_limit_bytes": 4194304,
        },
        "Interactive SSH PTY broker settings.",
        onboarding_overridable=False,
    )


@dataclass(frozen=True)
class WebServicesDefaultValues:
    crawl4ai: DefaultValue[dict[str, Any]] = default_value(
        {
            "base_url": "http://127.0.0.1:11235",
            "timeout_seconds": 120.0,
        },
        "Address of the instance's Crawl4AI server and the per-request timeout; its token is a linked account.",
        onboarding_overridable=False,
    )
    searxng: DefaultValue[dict[str, Any]] = default_value(
        {
            "base_url": "http://127.0.0.1:8080",
            "verify_tls": True,
            "timeout_seconds": 30.0,
            "default_num_results": 8,
        },
        "SearXNG service connection and bounded search-result settings.",
        onboarding_overridable=False,
    )


@dataclass(frozen=True)
class AccessWorkflowDefaultValues:
    max_steps: DefaultValue[int] = default_value(
        32, "Maximum steps in one access workflow an agent publishes.", onboarding_overridable=False,
    )
    diagnostic_event_limit: DefaultValue[int] = default_value(
        1000, "Maximum structured events in an owner-scoped workflow diagnostic download.", onboarding_overridable=False,
    )


@dataclass(frozen=True)
class LaunchDefaultValues:
    secrets_source: DefaultValue[str] = default_value(
        SECRETS_SOURCE_ENV_FILE,
        "Where secrets come from: env_file keeps them in the instance's team.env; "
        "environment reads only process environment variables and Docker secrets "
        "(*_FILE), and onboarding writes no team.env.",
        onboarding_overridable=True,
    )


@dataclass(frozen=True)
class ProjectDefaultValues:
    launch: LaunchDefaultValues = LaunchDefaultValues()
    transport: TransportDefaultValues = TransportDefaultValues()
    network: NetworkDefaultValues = NetworkDefaultValues()
    llm: LlmDefaultValues = LlmDefaultValues()
    workspace: WorkspaceDefaultValues = WorkspaceDefaultValues()
    storage: StorageDefaultValues = StorageDefaultValues()
    agent: AgentDefaultValues = AgentDefaultValues()
    embedding: EmbeddingDefaultValues = EmbeddingDefaultValues()
    conversation_compaction: ConversationCompactionDefaultValues = (
        ConversationCompactionDefaultValues()
    )
    gateway: GatewayDefaultValues = GatewayDefaultValues()
    delivery: DeliveryDefaultValues = DeliveryDefaultValues()
    heartbeat: HeartbeatDefaultValues = HeartbeatDefaultValues()
    interactive_apps: InteractiveAppDefaultValues = InteractiveAppDefaultValues()
    tool_policy: ToolPolicyDefaultValues = ToolPolicyDefaultValues()
    voice: VoiceDefaultValues = VoiceDefaultValues()
    activity_collector: ActivityCollectorDefaultValues = (
        ActivityCollectorDefaultValues()
    )
    otel: OtelDefaultValues = OtelDefaultValues()
    authorization: AuthorizationDefaultValues = AuthorizationDefaultValues()
    security_layer: SecurityLayerDefaultValues = SecurityLayerDefaultValues()
    execution_control: ExecutionControlDefaultValues = ExecutionControlDefaultValues()
    web_services: WebServicesDefaultValues = WebServicesDefaultValues()
    ui: UIDefaultValues = UIDefaultValues()
    access_workflows: AccessWorkflowDefaultValues = AccessWorkflowDefaultValues()


DEFAULT_VALUES = ProjectDefaultValues()

LAUNCH_DEFAULTS = DEFAULT_VALUES.launch
TRANSPORT_DEFAULTS = DEFAULT_VALUES.transport
NETWORK_DEFAULTS = DEFAULT_VALUES.network
LLM_DEFAULTS = DEFAULT_VALUES.llm
WORKSPACE_DEFAULTS = DEFAULT_VALUES.workspace
STORAGE_DEFAULTS = DEFAULT_VALUES.storage
AGENT_DEFAULTS = DEFAULT_VALUES.agent
EMBEDDING_DEFAULTS = DEFAULT_VALUES.embedding
CONVERSATION_COMPACTION_DEFAULTS = DEFAULT_VALUES.conversation_compaction
GATEWAY_DEFAULTS = DEFAULT_VALUES.gateway
DELIVERY_DEFAULTS = DEFAULT_VALUES.delivery
HEARTBEAT_DEFAULTS = DEFAULT_VALUES.heartbeat
INTERACTIVE_APP_DEFAULTS = DEFAULT_VALUES.interactive_apps
TOOL_POLICY_DEFAULTS = DEFAULT_VALUES.tool_policy
VOICE_DEFAULTS = DEFAULT_VALUES.voice
ACTIVITY_COLLECTOR_DEFAULTS = DEFAULT_VALUES.activity_collector
OTEL_DEFAULTS = DEFAULT_VALUES.otel
AUTHORIZATION_DEFAULTS = DEFAULT_VALUES.authorization
SECURITY_LAYER_DEFAULTS = DEFAULT_VALUES.security_layer
EXECUTION_CONTROL_DEFAULTS = DEFAULT_VALUES.execution_control
WEB_SERVICES_DEFAULTS = DEFAULT_VALUES.web_services
UI_DEFAULTS = DEFAULT_VALUES.ui
ACCESS_WORKFLOW_DEFAULTS = DEFAULT_VALUES.access_workflows
