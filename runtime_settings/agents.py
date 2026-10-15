"""Agents configuration and contracts."""

from __future__ import annotations
import copy
from functools import lru_cache
from typing import Any, Dict, Optional
from pydantic import BaseModel, Field, field_validator
from runtime_settings.knowledge import KnowledgeSettings
from runtime_settings.memory import MemoryConfig
from runtime_settings.schedule_workers import ScheduleWorkersConfig
from runtime_settings.worker_pools import WorkerPoolsConfig
from runtime_settings.review_cleanup import ReviewCleanupSettings
from runtime_settings.stale_schedules import StaleScheduleSettings
from runtime_settings.stuck_tasks import StuckTaskSettings
from default_values import AGENT_DEFAULTS
from runtime_settings.common import (
    _default_conversation_compactor_config,
    _merge_missing_defaults,
    _normalize_external_mcp_server_list,
)


TOOL_LIST_FIELDS = ("tool_allowlist", "tool_blocklist")


@lru_cache(maxsize=1)
def _tool_function_names() -> frozenset[str]:
    """The built-in tool functions an operator's allow and block lists can name (the release's tool catalog).

    MCP server tools are not filtered by these lists (custom_agents.tool_sync).
    Imported here, not at module load: the catalog reads runtime settings.
    """

    from access_catalog.catalog import load_catalog

    catalog = load_catalog(None)
    return frozenset(catalog.functions) | frozenset(catalog.function_dispatchers)


def check_tool_names(field: str, names: Optional[list[str]]) -> Optional[list[str]]:
    """Refuse a tool list entry that names no tool function.

    The lists match exact function names, so a misspelled or renamed entry
    matched nothing: a block list quietly stopped blocking, and an allow list
    quietly hid a tool.
    """

    if not names:
        return names
    unknown = sorted({str(name) for name in names} - _tool_function_names())
    if unknown:
        raise ValueError(
            f"agent.{field} names {', '.join(unknown)}, which {'is' if len(unknown) == 1 else 'are'} not a tool "
            "function in this version. The functions are listed in ua_definitions/access/tools/functions.yaml; "
            "MCP server tools are not filtered by this list."
        )
    return names


class AgentRuntimeDefaultsSettings(BaseModel):
    schedule_workers: ScheduleWorkersConfig = Field(default_factory=ScheduleWorkersConfig)
    worker_pools: WorkerPoolsConfig = Field(default_factory=WorkerPoolsConfig)
    max_deferred_messages: int = Field(
        default=AGENT_DEFAULTS.max_deferred_messages.value, ge=1
    )
    default_request_timezone: str = AGENT_DEFAULTS.default_request_timezone.value
    memory_graph_enabled: bool = AGENT_DEFAULTS.memory_graph_enabled.value
    memory: MemoryConfig = Field(
        default_factory=lambda: copy.deepcopy(AGENT_DEFAULTS.memory.value)
    )
    knowledge: KnowledgeSettings = Field(default_factory=KnowledgeSettings)
    schedule_maintenance: Dict[str, Any] = Field(
        default_factory=lambda: copy.deepcopy(AGENT_DEFAULTS.schedule_maintenance.value)
    )
    max_queued_top_level_tasks: int = AGENT_DEFAULTS.max_queued_top_level_tasks.value
    max_concurrent_top_level_tasks: int = (
        AGENT_DEFAULTS.max_concurrent_top_level_tasks.value
    )
    task_queue_warning_threshold: int = (
        AGENT_DEFAULTS.task_queue_warning_threshold.value
    )
    subtask_timeout_seconds: Optional[float] = (
        AGENT_DEFAULTS.subtask_timeout_seconds.value
    )
    subtask_watchdog_interval_seconds: float = (
        AGENT_DEFAULTS.subtask_watchdog_interval_seconds.value
    )
    subtask_status_probe_timeout_seconds: float = (
        AGENT_DEFAULTS.subtask_status_probe_timeout_seconds.value
    )
    restart_executing_task_grace_seconds: Optional[float] = (
        AGENT_DEFAULTS.restart_executing_task_grace_seconds.value
    )
    same_role_runtime_takeover_enabled: bool = (
        AGENT_DEFAULTS.same_role_runtime_takeover_enabled.value
    )
    max_tool_calls_per_task: Optional[int] = (
        AGENT_DEFAULTS.max_tool_calls_per_task.value
    )
    max_tool_result_tokens_per_call: Optional[int] = (
        AGENT_DEFAULTS.max_tool_result_tokens_per_call.value
    )
    runtime_budget_policy: Dict[str, Any] = Field(
        default_factory=lambda: copy.deepcopy(
            AGENT_DEFAULTS.runtime_budget_policy.value
        )
    )
    turn_action_outbox: Dict[str, Any] = Field(
        default_factory=lambda: copy.deepcopy(AGENT_DEFAULTS.turn_action_outbox.value)
    )
    runtime_coordinator: Dict[str, Any] = Field(
        default_factory=lambda: copy.deepcopy(AGENT_DEFAULTS.runtime_coordinator.value)
    )
    mid_turn_steering: Dict[str, Any] = Field(
        default_factory=lambda: copy.deepcopy(AGENT_DEFAULTS.mid_turn_steering.value)
    )
    inner_loop_context: Dict[str, Any] = Field(
        default_factory=lambda: copy.deepcopy(AGENT_DEFAULTS.inner_loop_context.value)
    )
    context_engine: Dict[str, Any] = Field(
        default_factory=lambda: copy.deepcopy(AGENT_DEFAULTS.context_engine.value)
    )
    debug: Dict[str, Any] = Field(
        default_factory=lambda: copy.deepcopy(AGENT_DEFAULTS.debug.value)
    )
    structured_output_failure_threshold: Optional[int] = (
        AGENT_DEFAULTS.structured_output_failure_threshold.value
    )
    deferred_message_warning_threshold: int = (
        AGENT_DEFAULTS.deferred_message_warning_threshold.value
    )
    conversation_compactor: Dict[str, Any] = Field(
        default_factory=_default_conversation_compactor_config
    )
    conversation_summary_generator_type: str = (
        AGENT_DEFAULTS.conversation_summary_generator_type.value
    )
    tool_allowlist: Optional[list[str]] = None
    tool_blocklist: list[str] = Field(default_factory=list)
    external_mcp_servers: list[Dict[str, Any]] = Field(default_factory=list)
    agent_definition_paths: list[str] = Field(default_factory=list)
    prompt_skill_description_exclusions: list[str] = Field(default_factory=list)
    prompt_skill_description_limit: int = Field(
        default=AGENT_DEFAULTS.prompt_skill_description_limit.value,
        ge=1,
    )
    result_transformer: Optional[str] = None

    @field_validator(*TOOL_LIST_FIELDS)
    @classmethod
    def _known_tool_names(cls, value: Optional[list[str]], info: Any) -> Optional[list[str]]:
        return check_tool_names(info.field_name, value)

    @field_validator("external_mcp_servers", mode="before")
    @classmethod
    def _normalize_external_mcp_servers(cls, value: Any) -> Any:
        return _normalize_external_mcp_server_list(value, preserve_none=False)

    @field_validator("memory", mode="before")
    @classmethod
    def _merge_memory_defaults(cls, value: Any) -> Any:
        return _merge_missing_defaults(
            value or {},
            copy.deepcopy(AGENT_DEFAULTS.memory.value),
        )

    @field_validator("schedule_maintenance", mode="before")
    @classmethod
    def _merge_schedule_maintenance_defaults(cls, value: Any) -> Any:
        merged = _merge_missing_defaults(
            value or {},
            copy.deepcopy(AGENT_DEFAULTS.schedule_maintenance.value),
        )
        merged["memory_review_cleanup"] = ReviewCleanupSettings.model_validate(
            merged.get("memory_review_cleanup") or {}
        ).model_dump()
        merged["stale_schedules"] = StaleScheduleSettings.model_validate(
            merged.get("stale_schedules") or {}
        ).model_dump()
        merged["stuck_tasks"] = StuckTaskSettings.model_validate(
            merged.get("stuck_tasks") or {}
        ).model_dump()
        return merged

    @field_validator("inner_loop_context", mode="before")
    @classmethod
    def _merge_inner_loop_context_defaults(cls, value: Any) -> Any:
        return _merge_missing_defaults(
            value or {},
            copy.deepcopy(AGENT_DEFAULTS.inner_loop_context.value),
        )

    @field_validator("runtime_budget_policy", mode="before")
    @classmethod
    def _merge_runtime_budget_policy_defaults(cls, value: Any) -> Any:
        return _merge_missing_defaults(
            value or {},
            copy.deepcopy(AGENT_DEFAULTS.runtime_budget_policy.value),
        )

    @field_validator("turn_action_outbox", mode="before")
    @classmethod
    def _merge_turn_action_outbox_defaults(cls, value: Any) -> Any:
        return _merge_missing_defaults(
            value or {},
            copy.deepcopy(AGENT_DEFAULTS.turn_action_outbox.value),
        )

    @field_validator("runtime_coordinator", mode="before")
    @classmethod
    def _merge_runtime_coordinator_defaults(cls, value: Any) -> Any:
        return _merge_missing_defaults(
            value or {},
            copy.deepcopy(AGENT_DEFAULTS.runtime_coordinator.value),
        )

    @field_validator("mid_turn_steering", mode="before")
    @classmethod
    def _merge_mid_turn_steering_defaults(cls, value: Any) -> Any:
        return _merge_missing_defaults(
            value or {},
            copy.deepcopy(AGENT_DEFAULTS.mid_turn_steering.value),
        )

    @field_validator("context_engine", mode="before")
    @classmethod
    def _merge_context_engine_defaults(cls, value: Any) -> Any:
        return _merge_missing_defaults(
            value or {},
            copy.deepcopy(AGENT_DEFAULTS.context_engine.value),
        )

    @field_validator("conversation_compactor", mode="before")
    @classmethod
    def _merge_conversation_compactor_defaults(cls, value: Any) -> Any:
        return _merge_missing_defaults(
            value or {},
            _default_conversation_compactor_config(),
        )

    @field_validator("debug", mode="before")
    @classmethod
    def _merge_debug_defaults(cls, value: Any) -> Any:
        return _merge_missing_defaults(
            value or {},
            copy.deepcopy(AGENT_DEFAULTS.debug.value),
        )


class AgentRuntimeOverrideSettings(BaseModel):
    schedule_worker_rules: Optional[Dict[str, Any]] = None

    @field_validator("schedule_worker_rules")
    @classmethod
    def validate_schedule_worker_rules(cls, value: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
        from custom_messages.contracts.schedule_worker_v1 import ScheduleWorkerRulesV1

        if value is not None:
            ScheduleWorkerRulesV1.model_validate(value)
        return value

    max_deferred_messages: Optional[int] = Field(default=None, ge=1)
    memory_graph_enabled: Optional[bool] = None
    memory: Optional[MemoryConfig] = None
    max_queued_top_level_tasks: Optional[int] = None
    max_concurrent_top_level_tasks: Optional[int] = None
    task_queue_warning_threshold: Optional[int] = None
    subtask_timeout_seconds: Optional[float] = None
    subtask_watchdog_interval_seconds: Optional[float] = None
    subtask_status_probe_timeout_seconds: Optional[float] = None
    restart_executing_task_grace_seconds: Optional[float] = None
    same_role_runtime_takeover_enabled: Optional[bool] = None
    max_tool_calls_per_task: Optional[int] = None
    max_tool_result_tokens_per_call: Optional[int] = None
    runtime_budget_policy: Optional[Dict[str, Any]] = None
    turn_action_outbox: Optional[Dict[str, Any]] = None
    runtime_coordinator: Optional[Dict[str, Any]] = None
    mid_turn_steering: Optional[Dict[str, Any]] = None
    inner_loop_context: Optional[Dict[str, Any]] = None
    context_engine: Optional[Dict[str, Any]] = None
    debug: Optional[Dict[str, Any]] = None
    structured_output_failure_threshold: Optional[int] = None
    deferred_message_warning_threshold: Optional[int] = None
    conversation_compactor: Optional[Dict[str, Any]] = None
    conversation_summary_generator_type: Optional[str] = None
    tool_allowlist: Optional[list[str]] = None
    tool_blocklist: Optional[list[str]] = None
    external_mcp_servers: Optional[list[Dict[str, Any]]] = None
    agent_definition_paths: Optional[list[str]] = None
    prompt_skill_description_exclusions: Optional[list[str]] = None
    prompt_skill_description_limit: Optional[int] = Field(default=None, ge=1)
    result_transformer: Optional[str] = None

    @field_validator(*TOOL_LIST_FIELDS)
    @classmethod
    def _known_tool_names(cls, value: Optional[list[str]], info: Any) -> Optional[list[str]]:
        return check_tool_names(info.field_name, value)

    @field_validator("external_mcp_servers", mode="before")
    @classmethod
    def _normalize_external_mcp_servers(cls, value: Any) -> Any:
        return _normalize_external_mcp_server_list(value, preserve_none=True)
