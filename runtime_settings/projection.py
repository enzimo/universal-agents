"""Projection configuration and contracts."""

from __future__ import annotations
from typing import Any
from pathlib import Path
from storage.config import StorageSettings
import copy
from runtime_settings.knowledge import KnowledgeSettings
from default_values import (
    ACTIVITY_COLLECTOR_DEFAULTS,
    AGENT_DEFAULTS,
    CONVERSATION_COMPACTION_DEFAULTS,
    EMBEDDING_DEFAULTS,
    LLM_DEFAULTS,
    OTEL_DEFAULTS,
    STORAGE_DEFAULTS,
    TRANSPORT_DEFAULTS,
)
from runtime_settings.governance import ToolPolicySettings
from runtime_settings.values import deep_merge_dicts
from runtime_settings.values import (
    _normalize_positive_float,
    _normalize_positive_int,
)


def _project_transport(*, transport_cfg: dict[str, Any]) -> dict[str, Any]:
    """Project resolved transport values without reading configuration again."""
    return {
        "transport_type": TRANSPORT_DEFAULTS.type.value,
        "nats_url": transport_cfg.get("nats_url", TRANSPORT_DEFAULTS.nats_url.value),
        "nats_servers": list(
            transport_cfg.get("nats_servers")
            or (
                [transport_cfg.get("nats_url")] if transport_cfg.get("nats_url") else []
            )
            or list(TRANSPORT_DEFAULTS.nats_servers.value)
        ),
        "nats_name": transport_cfg.get("nats_name", TRANSPORT_DEFAULTS.nats_name.value),
        "nats_jetstream_enabled": transport_cfg.get(
            "jetstream_enabled", TRANSPORT_DEFAULTS.jetstream_enabled.value
        ),
        "nats_subject_prefix": transport_cfg.get(
            "subject_prefix", TRANSPORT_DEFAULTS.subject_prefix.value
        ),
        "nats_storage": transport_cfg.get("storage", TRANSPORT_DEFAULTS.storage.value),
        "nats_replicas": transport_cfg.get(
            "replicas", TRANSPORT_DEFAULTS.replicas.value
        ),
        "nats_ack_wait_seconds": transport_cfg.get(
            "ack_wait_seconds", TRANSPORT_DEFAULTS.ack_wait_seconds.value
        ),
        "nats_max_deliver": transport_cfg.get(
            "max_deliver", TRANSPORT_DEFAULTS.max_deliver.value
        ),
        "nats_duplicate_window_seconds": transport_cfg.get(
            "duplicate_window_seconds",
            TRANSPORT_DEFAULTS.duplicate_window_seconds.value,
        ),
        "nats_work_max_age_seconds": transport_cfg.get(
            "work_max_age_seconds", TRANSPORT_DEFAULTS.work_max_age_seconds.value
        ),
        "nats_presence_max_age_seconds": transport_cfg.get(
            "presence_max_age_seconds",
            TRANSPORT_DEFAULTS.presence_max_age_seconds.value,
        ),
        "nats_process_consumer_inactive_threshold_seconds": transport_cfg.get(
            "process_consumer_inactive_threshold_seconds",
            TRANSPORT_DEFAULTS.process_consumer_inactive_threshold_seconds.value,
        ),
        "nats_user": transport_cfg.get("nats_user", TRANSPORT_DEFAULTS.nats_user.value),
        "nats_password": transport_cfg.get(
            "nats_password", TRANSPORT_DEFAULTS.nats_password.value
        ),
        "nats_credentials_file": transport_cfg.get(
            "credentials_file", TRANSPORT_DEFAULTS.credentials_file.value
        ),
    }


def _project_model(
    *,
    llm_cfg: dict[str, Any],
    llm_failover: dict[str, Any],
    resolved_llm_override_path: str | Path,
) -> dict[str, Any]:
    """Project resolved model values without reading configuration again."""
    return {
        "model_provider": llm_cfg.get("provider", LLM_DEFAULTS.provider.value),
        "model_id": llm_cfg.get("model_id", LLM_DEFAULTS.model_id.value),
        "temperature": llm_cfg.get("temperature", LLM_DEFAULTS.temperature.value),
        "reasoning_effort": llm_cfg.get(
            "reasoning_effort", LLM_DEFAULTS.reasoning_effort.value
        ),
        "reasoning_summary": llm_cfg.get(
            "reasoning_summary", LLM_DEFAULTS.reasoning_summary.value
        ),
        "max_output_tokens": llm_cfg.get(
            "max_output_tokens", LLM_DEFAULTS.max_output_tokens.value
        ),
        "context_window_tokens": llm_cfg.get(
            "context_window_tokens", LLM_DEFAULTS.context_window_tokens.value
        ),
        "stream": llm_cfg.get("stream", LLM_DEFAULTS.stream.value),
        "litellm_api_mode": llm_cfg.get(
            "litellm_api_mode", LLM_DEFAULTS.litellm_api_mode.value
        ),
        "request_timeout_seconds": _normalize_positive_float(
            llm_cfg.get(
                "request_timeout_seconds", LLM_DEFAULTS.request_timeout_seconds.value
            ),
            field_name="llm.request_timeout_seconds",
        ),
        "stream_idle_timeout_seconds": _normalize_positive_float(
            llm_cfg.get(
                "stream_idle_timeout_seconds",
                LLM_DEFAULTS.stream_idle_timeout_seconds.value,
            ),
            field_name="llm.stream_idle_timeout_seconds",
        ),
        "model_call_timeout_seconds": _normalize_positive_float(
            llm_cfg.get(
                "model_call_timeout_seconds",
                LLM_DEFAULTS.model_call_timeout_seconds.value,
            ),
            field_name="llm.model_call_timeout_seconds",
        ),
        "supports_image_input": llm_cfg.get(
            "supports_image_input", LLM_DEFAULTS.supports_image_input.value
        ),
        "supports_audio_input": llm_cfg.get(
            "supports_audio_input", LLM_DEFAULTS.supports_audio_input.value
        ),
        "supports_file_input": llm_cfg.get(
            "supports_file_input", LLM_DEFAULTS.supports_file_input.value
        ),
        "model_capabilities": copy.deepcopy(
            llm_cfg.get("model_capabilities", LLM_DEFAULTS.model_capabilities.value)
            or {}
        ),
        "reasoning_trace_retention_messages": llm_cfg.get(
            "reasoning_trace_retention_messages",
            LLM_DEFAULTS.reasoning_trace_retention_messages.value,
        ),
        "llm_failover": llm_failover,
        "openai_codex": deep_merge_dicts(
            copy.deepcopy(LLM_DEFAULTS.openai_codex.value),
            dict(llm_cfg.get("openai_codex", {}) or {}),
        ),
        "xai_oauth": deep_merge_dicts(
            copy.deepcopy(LLM_DEFAULTS.xai_oauth.value),
            dict(llm_cfg.get("xai_oauth", {}) or {}),
        ),
        "litellm_api_base": llm_cfg.get(
            "litellm_api_base", LLM_DEFAULTS.litellm_api_base.value
        ),
        "llm_override_path": str(resolved_llm_override_path),
    }


def _project_agent(
    *,
    agent_cfg: dict[str, Any],
    authorization_cfg: dict[str, Any],
    debug: dict[str, Any],
    internal_website_base_url: str,
    internal_website_cfg: dict[str, Any],
    llm_cfg: dict[str, Any],
    mcp_cfg: dict[str, Any],
    memory_config: dict[str, Any],
    network_public_origin: str,
    objective_execution_enabled: bool,
    objective_execution_paths: list[str],
    instance_root: str,
    raw_config: dict[str, Any],
    resolved_storage: StorageSettings,
    resolved_team_composition_path: str | Path,
    resolved_team_templates_path: str | Path,
    schedule_maintenance: dict[str, Any],
    skill_installation_cfg: dict[str, Any],
    skills_dir: str,
    task_history_db_path: str,
    workspace_dir: str,
) -> dict[str, Any]:
    """Project resolved agent values without reading configuration again."""
    return {
        "compaction_model_provider": llm_cfg.get(
            "compaction_model_provider", LLM_DEFAULTS.compaction_model_provider.value
        ),
        "compaction_model_id": llm_cfg.get(
            "compaction_model_id", LLM_DEFAULTS.compaction_model_id.value
        ),
        "storage_backend": resolved_storage.backend,
        "storage_sqlite_path": resolved_storage.sqlite_path,
        "storage_sqlite_vector_enabled": resolved_storage.sqlite_vector.enabled
        if resolved_storage.sqlite_vector is not None
        else STORAGE_DEFAULTS.sqlite_vector_enabled.value,
        "storage_sqlite_vector_provider": resolved_storage.sqlite_vector.provider
        if resolved_storage.sqlite_vector is not None
        else STORAGE_DEFAULTS.sqlite_vector_provider.value,
        "storage_sqlite_vector_require_extension": resolved_storage.sqlite_vector.require_extension
        if resolved_storage.sqlite_vector is not None
        else STORAGE_DEFAULTS.sqlite_vector_require_extension.value,
        "storage_postgres_dsn": resolved_storage.postgres_dsn,
        "task_history_db_path": task_history_db_path,
        "default_request_timezone": agent_cfg.get(
            "default_request_timezone", AGENT_DEFAULTS.default_request_timezone.value
        ),
        "skills_dir": skills_dir,
        "objective_execution_enabled": objective_execution_enabled,
        "objective_execution_paths": objective_execution_paths,
        "skill_installation_config": skill_installation_cfg,
        "instance_root": instance_root,
        "workspace_dir": workspace_dir,
        "instance_config_path": agent_cfg.get("instance_config_path"),
        "bootstrap_config": dict(agent_cfg.get("bootstrap", {}) or {}),
        "authorization_config": authorization_cfg,
        # The manager deletes older files from scratch/ folders when the
        # internal-improvement maintenance schedule fires.
        "scratch_retention_days": ToolPolicySettings.model_validate(
            dict(raw_config.get("tool_policy") or {})
        ).scratch_retention_days,
        "team_templates_path": str(resolved_team_templates_path),
        "team_composition_path": str(resolved_team_composition_path),
        "mcp_profiles": dict(raw_config.get("mcp_profiles") or {}),
        "mcp_config": dict(mcp_cfg),
        "external_mcp_servers": list(agent_cfg.get("external_mcp_servers", []) or []),
        "agent_definition_paths": list(
            agent_cfg.get("agent_definition_paths", []) or []
        ),
        "prompt_skill_description_exclusions": list(
            agent_cfg.get("prompt_skill_description_exclusions", []) or []
        ),
        "prompt_skill_description_limit": _normalize_positive_int(
            agent_cfg.get(
                "prompt_skill_description_limit",
                AGENT_DEFAULTS.prompt_skill_description_limit.value,
            ),
            field_name="agent.prompt_skill_description_limit",
        ),
        "tool_allowlist": agent_cfg.get("tool_allowlist"),
        "tool_blocklist": list(agent_cfg.get("tool_blocklist", []) or []),
        "network_public_origin": network_public_origin,
        "internal_website_base_url": internal_website_base_url,
        "internal_website_docs_root": internal_website_cfg.get("docs_root"),
        "internal_website_skills_dir": internal_website_cfg.get("skills_dir"),
        "agent_result_transformer_spec": agent_cfg.get("result_transformer"),
        "memory_graph_enabled": agent_cfg.get(
            "memory_graph_enabled", AGENT_DEFAULTS.memory_graph_enabled.value
        ),
        "memory_config": memory_config,
        "knowledge_config": KnowledgeSettings.model_validate(
            dict(agent_cfg.get("knowledge") or {})
        ).model_dump(),
        "schedule_maintenance": schedule_maintenance,
        "schedule_workers": dict(agent_cfg.get("schedule_workers", AGENT_DEFAULTS.schedule_workers.value)),
        "worker_pools": dict(agent_cfg.get("worker_pools", AGENT_DEFAULTS.worker_pools.value)),
        "max_queued_top_level_tasks": agent_cfg.get(
            "max_queued_top_level_tasks",
            AGENT_DEFAULTS.max_queued_top_level_tasks.value,
        ),
        "max_concurrent_top_level_tasks": agent_cfg.get(
            "max_concurrent_top_level_tasks",
            AGENT_DEFAULTS.max_concurrent_top_level_tasks.value,
        ),
        "task_queue_warning_threshold": agent_cfg.get(
            "task_queue_warning_threshold",
            AGENT_DEFAULTS.task_queue_warning_threshold.value,
        ),
        "subtask_timeout_seconds": agent_cfg.get(
            "subtask_timeout_seconds", AGENT_DEFAULTS.subtask_timeout_seconds.value
        ),
        "subtask_watchdog_interval_seconds": agent_cfg.get(
            "subtask_watchdog_interval_seconds",
            AGENT_DEFAULTS.subtask_watchdog_interval_seconds.value,
        ),
        "subtask_status_probe_timeout_seconds": agent_cfg.get(
            "subtask_status_probe_timeout_seconds",
            AGENT_DEFAULTS.subtask_status_probe_timeout_seconds.value,
        ),
        "restart_executing_task_grace_seconds": agent_cfg.get(
            "restart_executing_task_grace_seconds",
            AGENT_DEFAULTS.restart_executing_task_grace_seconds.value,
        ),
        "same_role_runtime_takeover_enabled": agent_cfg.get(
            "same_role_runtime_takeover_enabled",
            AGENT_DEFAULTS.same_role_runtime_takeover_enabled.value,
        ),
        "max_tool_calls_per_task": agent_cfg.get(
            "max_tool_calls_per_task", AGENT_DEFAULTS.max_tool_calls_per_task.value
        ),
        "max_tool_result_tokens_per_call": agent_cfg.get(
            "max_tool_result_tokens_per_call",
            AGENT_DEFAULTS.max_tool_result_tokens_per_call.value,
        ),
        "runtime_budget_policy": dict(
            agent_cfg.get(
                "runtime_budget_policy", AGENT_DEFAULTS.runtime_budget_policy.value
            )
            or {}
        ),
        "turn_action_outbox": dict(
            agent_cfg.get("turn_action_outbox", AGENT_DEFAULTS.turn_action_outbox.value)
            or {}
        ),
        "runtime_coordinator": dict(
            agent_cfg.get(
                "runtime_coordinator", AGENT_DEFAULTS.runtime_coordinator.value
            )
            or {}
        ),
        "mid_turn_steering": dict(
            agent_cfg.get("mid_turn_steering", AGENT_DEFAULTS.mid_turn_steering.value)
            or {}
        ),
        "debug": debug,
        "structured_output_failure_threshold": agent_cfg.get(
            "structured_output_failure_threshold",
            AGENT_DEFAULTS.structured_output_failure_threshold.value,
        ),
        "deferred_message_warning_threshold": agent_cfg.get(
            "deferred_message_warning_threshold",
            AGENT_DEFAULTS.deferred_message_warning_threshold.value,
        ),
        "max_deferred_messages": agent_cfg.get(
            "max_deferred_messages", AGENT_DEFAULTS.max_deferred_messages.value
        ),
    }


def _project_security(
    *,
    credential_catalog: dict[str, Any],
    credential_state_backend: str,
    sealbox: dict[str, Any],
    secure_tool_broker_bind_host: str,
    secure_tool_broker_bind_port: int,
    secure_tool_broker_credentials_dir: str,
    secure_tool_broker_internal_base_url: str,
    secure_tool_broker_public_base_url: str,
    secure_tool_broker_public_port: int,
    security_layer_mode: str,
    code_execution: Any,
) -> dict[str, Any]:
    """Project resolved security values without reading configuration again."""
    return {
        "security_layer_mode": security_layer_mode,
        "code_sandbox_mode": code_execution.sandbox,
        "code_sandbox_extra_read_paths": list(code_execution.extra_read_paths),
        "code_sandbox_extra_write_paths": list(code_execution.extra_write_paths),
        "secure_tool_broker_internal_base_url": secure_tool_broker_internal_base_url,
        "secure_tool_broker_public_port": secure_tool_broker_public_port,
        "secure_tool_broker_public_base_url": secure_tool_broker_public_base_url,
        "secure_tool_broker_bind_host": secure_tool_broker_bind_host,
        "secure_tool_broker_bind_port": secure_tool_broker_bind_port,
        "secure_tool_broker_credentials_dir": secure_tool_broker_credentials_dir,
        "secure_tool_broker_credential_state_backend": credential_state_backend,
        "secure_tool_broker_credential_catalog": credential_catalog,
        "secure_tool_broker_sealbox": sealbox,
    }


def _project_embedding(*, embedding_cfg: dict[str, Any]) -> dict[str, Any]:
    """Project resolved embedding values without reading configuration again."""
    return {
        "embedding_provider": embedding_cfg.get(
            "provider", EMBEDDING_DEFAULTS.provider.value
        ),
        "embedding_model_id": embedding_cfg.get(
            "model_id", EMBEDDING_DEFAULTS.model_id.value
        ),
        "embedding_api_base": embedding_cfg.get(
            "api_base", EMBEDDING_DEFAULTS.api_base.value
        ),
        "embedding_dimensions": embedding_cfg.get("dimensions"),
        "embedding_timeout_seconds": embedding_cfg.get(
            "timeout_seconds", EMBEDDING_DEFAULTS.timeout_seconds.value
        ),
        "embedding_organization": embedding_cfg.get(
            "organization", EMBEDDING_DEFAULTS.organization.value
        ),
        "embedding_extra_headers": dict(
            embedding_cfg.get("extra_headers", EMBEDDING_DEFAULTS.extra_headers.value)
            or {}
        ),
    }


def _project_context(
    *,
    agent_cfg: dict[str, Any],
    compactor_cfg: dict[str, Any],
    compactor_options: dict[str, Any],
    context_engine: dict[str, Any],
    inner_loop_context: dict[str, Any],
) -> dict[str, Any]:
    """Project resolved context values without reading configuration again."""
    return {
        "inner_loop_context": inner_loop_context,
        "context_engine": context_engine,
        "conversation_compactor_type": compactor_cfg.get(
            "type",
            agent_cfg.get(
                "conversation_compactor_type",
                CONVERSATION_COMPACTION_DEFAULTS.compactor_type.value,
            ),
        ),
        "conversation_summary_generator_type": agent_cfg.get(
            "conversation_summary_generator_type",
            AGENT_DEFAULTS.conversation_summary_generator_type.value,
        ),
        "context_compaction_trigger_ratio": compactor_options.get(
            "trigger_ratio",
            agent_cfg.get(
                "context_compaction_trigger_ratio",
                CONVERSATION_COMPACTION_DEFAULTS.trigger_ratio.value,
            ),
        ),
        "context_compaction_target_ratio": compactor_options.get(
            "target_ratio",
            agent_cfg.get(
                "context_compaction_target_ratio",
                CONVERSATION_COMPACTION_DEFAULTS.target_ratio.value,
            ),
        ),
        "context_compaction_min_recent_turns": compactor_options.get(
            "min_recent_turns",
            agent_cfg.get(
                "context_compaction_min_recent_turns",
                CONVERSATION_COMPACTION_DEFAULTS.min_recent_turns.value,
            ),
        ),
        "context_compaction_max_recent_turns": compactor_options.get(
            "max_recent_turns",
            agent_cfg.get(
                "context_compaction_max_recent_turns",
                CONVERSATION_COMPACTION_DEFAULTS.max_recent_turns.value,
            ),
        ),
        "context_compaction_trigger_turn_count": compactor_options.get(
            "trigger_turn_count", agent_cfg.get("context_compaction_trigger_turn_count")
        ),
        "context_compaction_trigger_message_count": compactor_options.get(
            "trigger_message_count",
            agent_cfg.get("context_compaction_trigger_message_count"),
        ),
    }


def _project_telemetry(
    *, activity_collector_cfg: dict[str, Any], otel_cfg: dict[str, Any]
) -> dict[str, Any]:
    """Project resolved telemetry values without reading configuration again."""
    return {
        "activity_collector_base_url": activity_collector_cfg.get(
            "base_url", ACTIVITY_COLLECTOR_DEFAULTS.base_url.value
        ),
        "activity_collector_connect_timeout_seconds": _normalize_positive_float(
            activity_collector_cfg.get(
                "connect_timeout_seconds",
                ACTIVITY_COLLECTOR_DEFAULTS.connect_timeout_seconds.value,
            ),
            field_name="activity_collector.connect_timeout_seconds",
        ),
        "activity_collector_request_timeout_seconds": _normalize_positive_float(
            activity_collector_cfg.get(
                "request_timeout_seconds",
                ACTIVITY_COLLECTOR_DEFAULTS.request_timeout_seconds.value,
            ),
            field_name="activity_collector.request_timeout_seconds",
        ),
        "activity_collector_batch_max_events": _normalize_positive_int(
            activity_collector_cfg.get(
                "batch_max_events", ACTIVITY_COLLECTOR_DEFAULTS.batch_max_events.value
            ),
            field_name="activity_collector.batch_max_events",
            max_value=1000,
        ),
        "activity_collector_flush_interval_seconds": _normalize_positive_float(
            activity_collector_cfg.get(
                "flush_interval_seconds",
                ACTIVITY_COLLECTOR_DEFAULTS.flush_interval_seconds.value,
            ),
            field_name="activity_collector.flush_interval_seconds",
        ),
        "activity_collector_max_buffered_events": _normalize_positive_int(
            activity_collector_cfg.get(
                "max_buffered_events",
                ACTIVITY_COLLECTOR_DEFAULTS.max_buffered_events.value,
            ),
            field_name="activity_collector.max_buffered_events",
            max_value=100000,
        ),
        "activity_collector_drop_when_unavailable": activity_collector_cfg.get(
            "drop_when_unavailable",
            ACTIVITY_COLLECTOR_DEFAULTS.drop_when_unavailable.value,
        ),
        "activity_collector_ingest_token_env_var": activity_collector_cfg.get(
            "ingest_token_env_var",
            ACTIVITY_COLLECTOR_DEFAULTS.ingest_token_env_var.value,
        ),
        "otel_enabled": otel_cfg.get("enabled", OTEL_DEFAULTS.enabled.value),
        "otel_enable_traces": otel_cfg.get(
            "enable_traces", OTEL_DEFAULTS.enable_traces.value
        ),
        "otel_enable_metrics": otel_cfg.get(
            "enable_metrics", OTEL_DEFAULTS.enable_metrics.value
        ),
        "otel_otlp_endpoint": otel_cfg.get(
            "otlp_endpoint", OTEL_DEFAULTS.otlp_endpoint.value
        ),
        "otel_service_name": otel_cfg.get(
            "service_name", OTEL_DEFAULTS.service_name.value
        ),
        "otel_headers": dict(
            otel_cfg.get("headers", OTEL_DEFAULTS.headers.value) or {}
        ),
    }
