"""
Universal Strands-backed agent runtime.

This module provides the UniversalAgent class, which:
- Connects to the configured message broker for asynchronous message handling
- Uses Strands Agent for LLM reasoning and tool execution
- Preserves the structured message protocol from the original system
- Supports the hub-and-spoke multi-agent architecture
"""

from custom_agents.lease_loss_wind_down import divert_request_during_wind_down, is_winding_down
from custom_agents.worker_handback_runtime import handle_worker_task_handback
from custom_agents.task_queue_notifications import QueuePressureNotifications
from custom_agents.dead_letter_watch import DeadLetterWatchState
from custom_agents.task_admission_runtime import handle_busy_delivery
from custom_agents import task_steering_runtime
from runtime_hosting.blocking_io import BlockingIOLane
from runtime_hosting.agent_execution import AgentExecutionGate
from custom_agents.sdk.strands.turn_ports import (
    TurnRuntimeSettings,
    TurnRuntimeEffects,
    TurnTaskOperations,
)
from custom_agents.task_runtime_ports import RuntimeMailboxDispatch
import asyncio
import contextlib
import copy
import inspect
import json
import logging
import os
import re
from datetime import datetime, timedelta, timezone
from functools import partial
from typing import Any, AsyncIterator, Callable, Dict, List, Literal, Mapping, Optional, Tuple, Union

import randomname
from default_values import (
    ACTIVITY_COLLECTOR_DEFAULTS,
    AGENT_DEFAULTS,
    CONVERSATION_COMPACTION_DEFAULTS,
    EMBEDDING_DEFAULTS,
    LLM_DEFAULTS,
    OTEL_DEFAULTS,
    TOOL_POLICY_DEFAULTS,
    TRANSPORT_DEFAULTS,
)
from custom_messages.contracts.runtime_v1 import (
    INTERNAL_IMPROVEMENT_AGENT_TYPE,
    MEM_REVIEW_AGENT_TYPE,
    TEAM_ARCHITECT_AGENT_TYPE,
)
from custom_agents.deferred_message_runtime import (
    DeferredHandoff,
    DeferredInboxOwner,
    DeferredMessageInbox,
    requires_execution_slot,
    is_priority_deferred_message,
    pending_deferred_handoff,
)
from custom_agents.agent_team_runtime import LocalAgentTeamRuntime, unregister_local_worker
from custom_agents.liveness_observation import LivenessObservation, NATS_PRESENCE_HEARTBEAT_SECONDS
from custom_agents.agent_liveness import own_process_identity, received_status_time
from custom_agents.custody_loss import install_custody_listeners
from storage.runtime_custody import DeferredMessageClaim, RuntimeCustodyLost
from custom_agents.runtime_paths import (
    agent_layout,
    fill_missing_instance_paths,
    infer_instance_root,
)
from custom_agents.model_runtime_profile import (
    MODEL_CAPABILITY_FIELDS,
    MODEL_TUNING_FIELDS,
    RUNTIME_LLM_REFERENCES,
    RuntimeLLMProfile,
    runtime_llm_profile_from_agent,
)
from env_secrets import get_env_with_file_fallback
from pydantic import BaseModel, ConfigDict, Field, PrivateAttr
from strands import Agent
from strands.agent.agent_result import AgentResult

from custom_agents.activity_client import (
    ActivityCollectorClient,
    ActivityCollectorClientSettings,
)
from config_loader import (
    load_persistent_llm_override,
    lookup_configured_model_capabilities,
    lookup_configured_model_request_constraints,
)
from custom_agents.embedding_service import (
    EmbeddingServiceClient,
    create_embedding_service_settings,
)
from custom_agents.model_keys import embedding_endpoint_key
from custom_agents.person_model_runtime import (
    PersonModelHook,
    clear_turn_model,
    select_turn_model,
    turn_settings_with_person_model,
)
from custom_agents.status_findings import agent_status_findings
from custom_agents.memory_graph_embedding_profile_runtime import (
    build_embedding_profile_spec,
    ensure_memory_graph_embedding_profile,
)
from custom_agents.memory_review_runtime import memory_review_notification_instruction
from custom_agents.memory_plugins.manager import MemoryPluginManager
from runtime_settings.memory import MemoryConfig
from custom_agents.automation_runtime import handle_automation_message
from custom_agents.agent_instructions import MESSAGE_FILL_NAME
from custom_agents.agent_result_transformers import load_agent_result_transformer
from custom_agents.agent_state_store import AgentStateStore
from custom_agents.conversation_runtime import (
    build_conversation_prompt_context,
    build_turn_summary,
    finalize_conversation_turn,
)
from custom_agents.conversation_summary import (
    ConversationSummaryConfig,
    ConversationSummaryGenerator,
    build_conversation_summary_generator,
)
from custom_agents.memory_review_store import MemoryReviewStore
from custom_agents.internal_improvement_runtime import (
    publish_internal_improvement_schedule_ensure,
)
from custom_agents.manager_control_runtime import (
    handle_manager_memory_command,
    handle_manager_runtime_work_command,
    handle_manager_schedule_command,
    handle_manager_topic_command,
    handle_task_progress_status,
)
from custom_agents.manager_bootstrap_runtime import run_manager_bootstrap
from custom_agents.sdk.strands.skills import (
    is_agent_skills_plugin,
    list_agent_skill_names,
    refresh_skills_plugin,
)
from custom_agents.skill_catalog_runtime import (
    SkillCatalogRuntimeState,
    handle_managed_skill_catalog_notification,
    repair_agent_skill_catalog,
)
from custom_agents.message_routing import (
    build_outgoing_meta,
    construct_header,
    resolve_outgoing_routing,
    resolve_requester_routing_key,
)
from custom_agents.request_prompt_context import (
    build_request_conversation_context,
    build_request_time_context,
    effective_default_request_timezone,
    ensure_request_timezone,
)
from custom_agents.otel_runtime import configure_strands_otel, current_trace_context
from custom_agents.runtime_model_switch import probe_strands_model
from custom_agents.runtime_guardrails import RuntimeGuardrailsPlugin
from custom_agents.tool_call_limits import (
    ToolCallLimitExceeded,
    collect_recent_strands_tool_transcript,
    compact_debug_preview as _compact_debug_preview,
    format_tool_count_summary,
)
from custom_agents.runtime_budget_policy import (
    friendly_budget_reason,
    max_user_guided_pauses_per_task,
    parse_runtime_pause_decision,
    partial_tool_call_budget_multiplier,
    partial_tool_call_limit,
    provider_overload_pause_prompt,
    runtime_budget_policy_config,
    runtime_budget_policy_mode,
    runtime_budget_trigger_enabled,
    runtime_pause_prompt,
    runtime_pause_identity,
)
from custom_agents.runtime_recovery import (
    discard_runtime_state_for_team_change,
    fail_recovered_executing_task,
    recover_runtime_after_restart,
    should_fail_stale_executing_recovery,
)
from custom_agents.scheduled_run_contexts import subscribe_run_context_routes
from custom_agents.scheduled_runtime import (
    enqueue_fired_scheduled_task,
    enqueue_heartbeat_items,
    handle_scheduled_message,
    scheduler_loop,
)
from custom_agents.heartbeat_store import is_heartbeat_launcher
from custom_agents.control_runtime import (
    _interrupt_active_execution,
    cancel_task_tree_locally,
    request_subtask_stop,
    resolve_control_target_task_info,
    send_control_status_response,
    send_task_cancel_response,
    stop_task_execution,
)
from custom_agents.conversation_compaction import (
    ConversationCompactionConfig,
    ConversationCompactor,
    build_conversation_compactor,
)
from custom_agents.strands_model_factory import (
    StrandsModelFactoryConfig,
    build_strands_model,
    resolve_context_window_tokens,
)
from custom_agents.conversation_store import ConversationStore
from custom_agents.context_engine import (
    ContextEngine,
    ContextEngineAdapters,
    DefaultContextEngine,
    TurnFinalizationRequest,
    build_context_engine_plugins,
)
from custom_agents.access_runtime import authorization_enabled, verified_meta_person_id
from custom_agents.context_engine.config import merge_context_engine_config
from custom_agents.context_engine.default import context_engine_kwargs_from_config
from custom_agents.context_debug_dump import (
    ContextDebugDumpPlugin,
    merge_context_debug_dump_config,
    register_context_debug_model_observer,
)
from custom_agents.filtered_tool_result_context import (
    merge_inner_loop_context_config,
)
from custom_agents.tool_result_offloader import TaskAwareToolResultOffloader
from custom_agents import task_message_runtime
from custom_agents import tool_setup_runtime
from custom_agents.person_role_context import person_role_context
from custom_agents.pending_work_runtime import (
    close_pending_work,
    schedule_pending_work,
    start_owned_task,
    wake_after_slot_release,
)
from custom_agents.objective_work_completion import prepare_objective_work_completion
from custom_agents.sdk.strands import turn as llm_turn_runtime
from custom_agents import model_failover_runtime
from custom_agents import delegation_runtime
from custom_agents import subtask_watchdog_runtime
from custom_agents import turn_action_outbox_runtime
from custom_agents.turn_action_outbox_runtime import TurnActionRecoveryScheduled
from custom_agents.task_steering_runtime import (
    RuntimeSteeringPlugin,
    StrandsInterruptedTurnSession,
    handle_task_steering_message,
    resume_interrupted_task_turn,
)
from custom_agents.pool_intake import mailbox_has_room, may_take_pool_task
from custom_agents.provider_limits import handle_delegated_provider_limit
from custom_agents.scheduled_runtime import PROVIDER_COOLDOWN_RETRY_MARKER
from custom_transport.base import (
    ConsumerScope,
    FetchPolicy,
    InboundDelivery,
    SubscriptionDelivery,
    TransportPublishOptions,
)
from custom_transport.nats_adapter import NAMED_WORKER_CONSUMER_ROLE, NatsTransportAdapter
from custom_transport.subjects import NatsSubjectMapper
from custom_agents.task_runtime_coordinator import (
    RuntimeBatchInterrupted,
    TaskRuntimeCoordinator,
)
from custom_agents.schedule_store import ScheduleStore, schedule_claim_scope_for_agent
from custom_agents.structured_llm_messages import (
    StructuredAgentTurnDecision,
    TurnAction,
)
from custom_agents.task_manager import TaskInfo, TaskManager
from custom_messages.contracts.activity_v1 import TaskActivityEvent
from custom_messages.contracts.control_v1 import (
    AgentCreationRequest,
    AgentStatus,
    AgentStatusMap,
    ControlInstruction,
    ManagerKnowledgeCommand,
    ManagerMemoryCommand,
    ManagerRuntimeWorkCommand,
    ManagerTopicCommand,
    PersonStandingCommandV1,
)
from custom_messages.contracts.credentials_v1 import (
    SecureCredentialCollectionRequest,
    SecureCredentialCollectionResult,
    ToolSetupRequestV1,
    ToolSetupResultV1,
    SignupAutomationRequest,
)
from custom_messages.contracts.delivery_v1 import DeliveryReceipt
from custom_messages.contracts.envelope_v1 import (
    CommsMessage,
    ContentType,
    MessageEnvelope,
)
from custom_messages.contracts.routing_v1 import (
    GatewayContext,
    MessageHeader,
    MessageMeta,
)
from custom_messages.contracts.schedule_v1 import (
    AutomationMessage,
    ManagerScheduleCommand,
    ScheduledMessage,
)
from custom_messages.contracts.task_v1 import (
    AfterActionReport,
    Task,
    TaskAnswer,
    TaskHistoryEntry,
    TaskQuery,
    TaskProgressStatus,
    TaskSteering,
    TaskStatus,
)
from custom_messages.enums import (
    ControlType,
    MessageMetaType,
    PlanActionStatus,
    PlanActionType,
    ProgressState,
    RequesterType,
    RoutingType,
    ScheduleManagementClass,
    TaskActivityStage,
    TaskActivityType,
    TaskHistoryImportance,
    TaskHistorySource,
    TaskHistoryType,
    TaskQueryDecision,
    TaskQueryReason,
    SystemScheduleKind,
    RecoverablePauseTrigger,
)
from custom_messages.skill_catalog import ManagedSkillCatalogChangedV1
from custom_messages.system_schedules import (
    AFTER_ACTION_REPORT_REVIEW_SCHEDULE_NAME_PREFIX,
    PROVIDER_OVERLOAD_RETRY_SCHEDULE_NAME_PREFIX,
)
from custom_messages.contracts.delegated_work_v1 import DelegatedTaskClaimedV1
from custom_messages.contracts.access_v2 import AccessRequestResolvedV1
from custom_agents.access_request_runtime import handle_access_request_resolved
from custom_messages.contracts.worker_handback_v1 import WorkerTaskHandbackV1
from custom_tools.comms_tools import TeamComms
from custom_tools.tool_stop import ToolStop
from storage.memory_graph_repository import create_memory_graph_repository
from storage.pinned_memory_repository import create_pinned_memory_repository
from storage.knowledge_repository import create_knowledge_repository
from runtime_settings.knowledge import KnowledgeSettings, knowledge_settings
from id_generation import new_id

# Configure logging
logging.basicConfig(level=logging.INFO)


DEFAULT_TEAM_ID = AGENT_DEFAULTS.no_team_id.value
MARKDOWN_FENCE_PATTERN = re.compile(r"(?<!\\)```")
MARKDOWN_INLINE_CODE_PATTERN = re.compile(r"(?<!\\)`")
MARKDOWN_BOLD_PATTERN = re.compile(r"(?<!\\)\*\*")
MARKDOWN_SINGLE_STAR_PATTERN = re.compile(r"(?<!\\)\*")
MARKDOWN_BULLET_PATTERN = re.compile(r"(?m)^\s*\*\s+")
_UNSET = object()


def _optional_int_from_env(name: str) -> Optional[int]:
    raw_value = get_env_with_file_fallback(name)
    if raw_value in (None, ""):
        return None
    return int(raw_value)


def _task_answer_text(answer: Union[TaskAnswer, str, None]) -> str:
    if isinstance(answer, TaskAnswer):
        return str(answer.answer or "")
    if answer is None:
        return ""
    return str(answer)


def _has_unbalanced_markdown_delimiter(text: str) -> bool:
    if len(MARKDOWN_FENCE_PATTERN.findall(text)) % 2:
        return True

    without_fences = MARKDOWN_FENCE_PATTERN.sub("", text)
    if len(MARKDOWN_BOLD_PATTERN.findall(without_fences)) % 2:
        return True

    without_bold = MARKDOWN_BOLD_PATTERN.sub("", without_fences)
    without_bullets = MARKDOWN_BULLET_PATTERN.sub("", without_bold)
    stripped_without_bullets = without_bullets.strip()
    single_star_count = len(MARKDOWN_SINGLE_STAR_PATTERN.findall(without_bullets))
    if single_star_count % 2 and (
        stripped_without_bullets.startswith("*")
        or stripped_without_bullets.endswith("*")
    ):
        return True

    return len(MARKDOWN_INLINE_CODE_PATTERN.findall(without_fences)) % 2 == 1


def _terminal_answer_quality_error(
    answer: Union[TaskAnswer, str, None],
) -> Optional[str]:
    text = _task_answer_text(answer)
    stripped = text.strip()
    if not stripped:
        return "answer is empty"
    if not any(character.isalnum() for character in stripped):
        return "answer contains only markdown or punctuation"
    if _has_unbalanced_markdown_delimiter(stripped):
        return "answer contains dangling markdown delimiters"
    return None



def _local_named_agent_types(
    runtime: LocalAgentTeamRuntime | None,
    current_named_agent_type: str | None,
) -> tuple[str, ...]:
    """Return configured local types, or the safe direct-construction subset."""
    if runtime is not None:
        return runtime.agent_types()
    return tuple(
        dict.fromkeys(
            named_type
            for named_type in (current_named_agent_type, TEAM_ARCHITECT_AGENT_TYPE)
            if named_type
        )
    )


def _validated_agent_status_id(
    agent_status: AgentStatus,
    *,
    logger: logging.Logger,
    receiving_agent_id: str,
    msg_header: MessageHeader,
    msg_meta: MessageMeta,
) -> str | None:
    """Return a usable status identity or reject the malformed update observably."""
    if agent_status.self_id:
        return agent_status.self_id
    logger.error(
        "[%s] Ignoring AgentStatus without self_id. "
        "event=agent_status_identity_missing correlation_id=%s incoming_from_agent_id=%s",
        receiving_agent_id,
        getattr(msg_meta, "msg_correlation_id", None),
        getattr(msg_header, "from_agent_id", None),
    )
    return None


def _should_consume_broadcasts(
    process_broadcasts: bool | None,
    *,
    is_team_lead: bool,
) -> bool:
    """Resolve the explicit override before the team-lead default."""
    if process_broadcasts is not None:
        return bool(process_broadcasts)
    return is_team_lead


def _direct_consumer_scope(*, configured_fields: set[str]) -> ConsumerScope:
    """Map configured identity lifetime to direct-mailbox ownership."""

    if "id" in configured_fields:
        return ConsumerScope.ROUTE
    return ConsumerScope.PROCESS


async def _subscribe_manager_control_route(
    agent: Any,
    adapter: NatsTransportAdapter,
) -> None:
    """Install task controls independently of every agent's execution slot."""
    mapper = agent._nats_mapper()
    await adapter.subscribe_subject(
        name=f"{agent.id}:control",
        subject=mapper.agent_control_subject(agent.id),
        handler=agent.on_nats_delivery,
        durable_name=mapper.durable_name(f"agent_{agent.id}_control"),
        logical_route=agent.id,
        consumer_scope=_direct_consumer_scope(configured_fields=agent.model_fields_set),
        consumer_role="agent_control",
        owner_agent_id=agent.id,
    )
    agent._logger.info("[%s] NATS control route ready for %s.", agent.id, agent.id)
    # The team lead signs scheduled runs for worker processes without the
    # scheduler key; such a worker listens for its answers.
    await subscribe_run_context_routes(agent, adapter, mapper)


async def _publish_exit_presence(agent: Any) -> None:
    """Stop local team members before publishing the lead's final presence."""

    if agent.is_team_lead and agent.team_id and agent._local_team_runtime is not None:
        await asyncio.to_thread(
            agent._local_team_runtime.shutdown_agents,
            agent.team_id,
            agent.id,
            5.0,
        )
    await agent._publish_presence_status(is_exiting=True)


class UniversalAgent(BaseModel):
    """An agent that handles broker-delivered messages using the Strands SDK.

    This agent uses a task manager to handle assigned tasks. It can process a single
    top-level task at a time (received from the messaging queue), but this task
    might involve a chain of internal sub-tasks. Manager agents, for instance,
    can generate multiple sub-tasks that are dispatched to other agents for handling.

    This agent uses composition to wrap a Strands Agent internally, providing
    the LLM reasoning and tool execution capabilities while maintaining the
    structured message-handling runtime.
    """

    # Agent identification
    id: str = Field(
        default_factory=lambda: new_id("agent_id"),
        description="Unique agent identifier used for NATS routing",
    )
    name: str = Field(
        default_factory=lambda: f"{randomname.get_name()}",
        description="Name of the agent (default is a random adjective-noun combination)",
    )
    named_agent_type: Optional[str] = Field(
        default=None,
        description="Named type of the agent. Will be used during instantiation requests to look up and set agent's parameters (e.g. role, backstory, tools). Not unique - e.g: CodeWritingAgent",
    )

    # Agent configuration
    system_prompt: str = Field(
        default="",
        description="Combined system prompt including role, goal, and backstory",
    )
    role: str = Field(default="", description="Agent's role description")
    goal: str = Field(default="", description="Agent's goal/objective")
    backstory: str = Field(default="", description="Agent's backstory/context")

    # Team configuration
    team_id: Optional[str] = Field(
        default=DEFAULT_TEAM_ID,
        description="The team ID to which this agent belongs (NO_TEAM is the default)",
    )
    is_team_lead: bool = Field(
        default=False, description="Whether this agent is the team leader"
    )
    access_role: str = Field(
        default="worker",
        description="'system' for built-in maintenance agents (fixed duties, work with no person); otherwise 'worker'",
    )

    # Transport configuration
    transport_type: str = Field(
        default=TRANSPORT_DEFAULTS.type.value,
        description="Messaging transport backend. NATS/JetStream is the only supported runtime transport.",
    )
    nats_url: str = Field(default=TRANSPORT_DEFAULTS.nats_url.value)
    nats_servers: List[str] = Field(
        default_factory=lambda: list(TRANSPORT_DEFAULTS.nats_servers.value)
    )
    nats_name: Optional[str] = Field(default=TRANSPORT_DEFAULTS.nats_name.value)
    nats_jetstream_enabled: bool = Field(
        default=TRANSPORT_DEFAULTS.jetstream_enabled.value
    )
    nats_subject_prefix: str = Field(default=TRANSPORT_DEFAULTS.subject_prefix.value)
    nats_storage: str = Field(default=TRANSPORT_DEFAULTS.storage.value)
    nats_replicas: int = Field(default=TRANSPORT_DEFAULTS.replicas.value)
    nats_ack_wait_seconds: float = Field(
        default=TRANSPORT_DEFAULTS.ack_wait_seconds.value
    )
    nats_max_deliver: int = Field(default=TRANSPORT_DEFAULTS.max_deliver.value)
    nats_duplicate_window_seconds: float = Field(
        default=TRANSPORT_DEFAULTS.duplicate_window_seconds.value
    )
    nats_work_max_age_seconds: Optional[float] = Field(
        default=TRANSPORT_DEFAULTS.work_max_age_seconds.value
    )
    nats_presence_max_age_seconds: Optional[float] = Field(
        default=TRANSPORT_DEFAULTS.presence_max_age_seconds.value
    )
    nats_process_consumer_inactive_threshold_seconds: float = Field(
        default=TRANSPORT_DEFAULTS.process_consumer_inactive_threshold_seconds.value,
        gt=0,
    )
    nats_user: Optional[str] = Field(default=TRANSPORT_DEFAULTS.nats_user.value)
    nats_password: Optional[str] = Field(default=TRANSPORT_DEFAULTS.nats_password.value)
    nats_credentials_file: Optional[str] = Field(
        default=TRANSPORT_DEFAULTS.credentials_file.value
    )

    # Queue mappings
    named_agent_type_queue_map: Dict[str, str] = Field(
        default_factory=dict,
        description="Map of durable worker routes used when a task targets an agent type instead of a specific agent instance.",
    )
    user_queue_postfix: str = Field(default=AGENT_DEFAULTS.user_queue_postfix.value)
    user_name_postfix: str = Field(default=AGENT_DEFAULTS.user_name_postfix.value)
    user_input_queue_name: Optional[str] = Field(default=None)
    default_request_timezone: Optional[str] = Field(
        default_factory=lambda: os.getenv("DEFAULT_REQUEST_TIMEZONE"),
        description="Optional IANA timezone used for top-level requests that omit request_timezone.",
    )

    # Broadcast processing configuration
    broadcast_history_length: int = Field(
        default=AGENT_DEFAULTS.broadcast_history_length.value,
        description="Number of presence broadcasts to buffer in the exchange.",
    )
    max_broadcast_age_ms: int = Field(
        default=AGENT_DEFAULTS.max_broadcast_age_ms.value,
        description="Discard broadcast messages older than this threshold (in milliseconds).",
    )
    process_broadcasts: Optional[bool] = Field(
        default=None,
        description="Whether to consume broadcasts. Defaults to True for Team Leads and False for others.",
    )
    delete_fanout_on_exit: bool = Field(
        default_factory=lambda: (
            os.getenv(
                "FANOUT_DELETE_ON_EXIT",
                str(AGENT_DEFAULTS.delete_fanout_on_exit.value).lower(),
            ).lower()
            in ("true", "1", "yes")
        ),
        description="Whether the team lead should delete the fanout exchange on exit. Defaults to False (exchange persists). Set FANOUT_DELETE_ON_EXIT=true to enable.",
    )

    # Communication utilities
    team_comms: TeamComms = Field(
        default_factory=TeamComms, description="Team communications object"
    )

    # Agent state management
    paused: bool = Field(
        default=AGENT_DEFAULTS.paused.value,
        description="To handle pausing and resuming the agent",
    )

    # Agent tracking
    known_agents: AgentStatusMap = Field(
        default_factory=AgentStatusMap,
        description="Stores status of other known agents by ID",
    )

    # Task management - use standard Python logger, not AgentLogger which lacks .info()/.warning()/.error() methods
    task_manager: TaskManager = Field(
        default_factory=lambda: TaskManager(logger=logging.getLogger("TaskManager"))
    )

    # Tools configuration - list of @tool decorated functions or tool specs
    tools: List[Any] = Field(
        default_factory=list, description="List of tools available to this agent"
    )
    tool_filter: Dict[str, Any] = Field(
        default_factory=dict,
        description=(
            "The operator's tool settings (tool_allowlist, tool_blocklist, "
            "skill_installation_enabled), applied again to tools added from grants (custom_agents.tool_sync)."
        ),
    )
    plugins: List[Any] = Field(
        default_factory=list,
        description="List of Strands plugins available to this agent",
    )
    skills_dir: Optional[str] = Field(
        default=None,
        description="Filesystem path passed to the Strands AgentSkills plugin",
    )
    skill_installation_config: Dict[str, Any] = Field(
        default_factory=dict,
        description="Operator-owned quarantine, SkillSpector, and reviewed skill-installation settings.",
    )
    instance_root: Optional[str] = Field(
        default=None,
        description=(
            "Instance root. Control files, databases, runtime records and the agents' "
            "workspaces/ area are derived from it (runtime_settings.instance_layout)."
        ),
    )
    workspace_dir: Optional[str] = Field(
        default=None,
        description=(
            "The agents' area, <instance_root>/workspaces. Each task works in its "
            "person's folder there plus the shared team/ and agents/ folders."
        ),
    )
    instance_config_path: Optional[str] = Field(
        default=None,
        description="Optional onboarded instance_config.yaml path used for structured per-turn context.",
    )
    bootstrap_config: Dict[str, Any] = Field(
        default_factory=dict,
        description="Configuration-only values consumed by manager-owned bootstrap migrations.",
    )
    authorization_config: Dict[str, Any] = Field(
        default_factory=dict,
        description="Operator-owned authorization settings (``authorization`` in config); read by access_runtime.",
    )
    scratch_retention_days: int = Field(
        default=TOOL_POLICY_DEFAULTS.scratch_retention_days.value,
        ge=0,
        description=(
            "``tool_policy.scratch_retention_days``: the manager deletes older files from scratch/ folders "
            "when the internal-improvement maintenance schedule fires (custom_agents.scratch_retention); 0 keeps them."
        ),
    )
    llm_override_path: Optional[str] = Field(
        default=None,
        description="Optional explicit path to the persistent LLM override YAML file.",
    )
    team_templates_path: Optional[str] = Field(
        default=None,
        description="Optional explicit path to the workspace-backed team templates YAML file.",
    )
    team_composition_path: Optional[str] = Field(
        default=None,
        description="Optional explicit path to the workspace-backed team composition YAML file.",
    )
    mcp_config: Dict[str, Any] = Field(
        default_factory=dict,
        description="Operator-owned MCP transport, network, reconciliation, and hosting settings.",
    )
    mcp_configured_connection_ids: List[str] = Field(
        default_factory=list,
        description=(
            "Records of the operator's configured MCP servers this agent's configuration lists; with "
            "authorization off it loads the active ones (custom_agents.providers.mcp_connections)."
        ),
    )
    additional_skill_directories: List[str] = Field(
        default_factory=list,
        description="Additional skill directories attached to the Strands AgentSkills plugin",
    )
    skill_exclusions: List[str] = Field(
        default_factory=list,
        description="Skill names hidden from the AgentSkills available-skills menu",
    )
    prompt_skill_description_limit: int = Field(
        default=AGENT_DEFAULTS.prompt_skill_description_limit.value,
        ge=1,
        description="Maximum deduplicated skill descriptions shown in the prompt menu; all skills remain activatable",
    )
    memory_graph_enabled: bool = Field(
        default_factory=lambda: (
            os.getenv(
                "MEMORY_GRAPH_ENABLED",
                str(AGENT_DEFAULTS.memory_graph_enabled.value).lower(),
            ).lower()
            in ("true", "1", "yes")
        ),
        description="Enable graph-backed memory infrastructure where supported.",
    )
    memory_config: MemoryConfig = Field(
        default_factory=lambda: dict(AGENT_DEFAULTS.memory.value),
        description="Memory plugin manager, pinned memory, and domain memory configuration.",
    )
    knowledge_config: Dict[str, Any] = Field(
        default_factory=lambda: KnowledgeSettings().model_dump(),
        description="Knowledge base limits and blocked patterns (runtime_settings.knowledge, agent.knowledge).",
    )
    schedule_workers: Dict[str, Any] = Field(default_factory=lambda: dict(AGENT_DEFAULTS.schedule_workers.value))
    worker_pools: Dict[str, Any] = Field(
        default_factory=lambda: copy.deepcopy(AGENT_DEFAULTS.worker_pools.value),
        description="Worker pool sizes, scaling, reviews and progress notes (runtime_settings.worker_pools).",
    )
    schedule_maintenance: Dict[str, Any] = Field(
        default_factory=lambda: dict(AGENT_DEFAULTS.schedule_maintenance.value),
        description="Recurring internal maintenance schedule settings.",
    )
    embedding_provider: str = Field(
        default_factory=lambda: os.getenv(
            "EMBEDDING_PROVIDER", EMBEDDING_DEFAULTS.provider.value
        ),
        description="Embedding provider for graph ingestion/retrieval. Supported: local, openai_compatible.",
    )
    embedding_model_id: Optional[str] = Field(
        default_factory=lambda: (
            os.getenv("EMBEDDING_MODEL_ID", EMBEDDING_DEFAULTS.model_id.value or "")
            or None
        ),
        description="Embedding model identifier used when the provider is service-backed.",
    )
    embedding_api_base: Optional[str] = Field(
        default_factory=lambda: (
            os.getenv("EMBEDDING_API_BASE", EMBEDDING_DEFAULTS.api_base.value or "")
            or None
        ),
        description="Base URL for the embedding service when using a service-backed provider.",
    )
    embedding_dimensions: Optional[int] = Field(
        default_factory=lambda: _optional_int_from_env("EMBEDDING_DIMENSIONS"),
        description="Optional target embedding dimensions. Local embeddings default to 48 when unset; service-backed requests only send a dimensions hint when configured.",
    )
    embedding_timeout_seconds: float = Field(
        default_factory=lambda: float(
            os.getenv(
                "EMBEDDING_TIMEOUT_SECONDS",
                str(EMBEDDING_DEFAULTS.timeout_seconds.value),
            )
        ),
        description="HTTP timeout in seconds for embedding service requests.",
    )
    embedding_organization: Optional[str] = Field(
        default_factory=lambda: os.getenv(
            "EMBEDDING_ORGANIZATION", EMBEDDING_DEFAULTS.organization.value
        ),
        description="Optional organization header for compatible embedding services.",
    )
    embedding_extra_headers: Dict[str, str] = Field(
        default_factory=lambda: copy.deepcopy(EMBEDDING_DEFAULTS.extra_headers.value),
        description="Optional extra HTTP headers sent to the embedding service.",
    )

    # LLM configuration
    model_id: str = Field(
        default=LLM_DEFAULTS.model_id.value, description="Model identifier for the LLM"
    )
    model_provider: str = Field(
        default=LLM_DEFAULTS.provider.value,
        description=(
            "LLM provider: 'openai', 'openai-codex', 'xai', 'xai-oauth', "
            "'gemini', 'bedrock', 'anthropic', or 'litellm'"
        ),
    )
    temperature: float = Field(
        default=LLM_DEFAULTS.temperature.value, description="LLM temperature setting"
    )
    reasoning_effort: Optional[str] = Field(
        default=LLM_DEFAULTS.reasoning_effort.value,
        description="Optional reasoning-effort hint translated to the selected provider transport.",
    )
    reasoning_summary: Optional[str] = Field(
        default=LLM_DEFAULTS.reasoning_summary.value,
        description="Optional OpenAI Responses reasoning summary: auto, concise, or detailed.",
    )
    max_output_tokens: Optional[int] = Field(
        default=LLM_DEFAULTS.max_output_tokens.value,
        description="Optional maximum generated tokens for model calls when supported by the provider adapter.",
    )
    stream: Optional[bool] = Field(
        default=LLM_DEFAULTS.stream.value,
        description=(
            "Optional streaming override for the LiteLLM provider adapter. "
            "None keeps the adapter default."
        ),
    )
    litellm_api_mode: str = Field(
        default=LLM_DEFAULTS.litellm_api_mode.value,
        description="LiteLLM proxy transport: chat_completions or responses.",
    )
    request_timeout_seconds: Optional[float] = Field(
        default=LLM_DEFAULTS.request_timeout_seconds.value,
        description=LLM_DEFAULTS.request_timeout_seconds.description,
        gt=0,
    )
    stream_idle_timeout_seconds: float = Field(
        default=LLM_DEFAULTS.stream_idle_timeout_seconds.value,
        description=LLM_DEFAULTS.stream_idle_timeout_seconds.description,
        gt=0,
    )
    model_call_timeout_seconds: float = Field(
        default=LLM_DEFAULTS.model_call_timeout_seconds.value,
        description=LLM_DEFAULTS.model_call_timeout_seconds.description,
        gt=0,
    )
    supports_image_input: Optional[bool] = Field(
        default=LLM_DEFAULTS.supports_image_input.value,
        description="Whether the active provider/model accepts image inputs. None means unknown.",
    )
    supports_audio_input: Optional[bool] = Field(
        default=LLM_DEFAULTS.supports_audio_input.value,
        description="Whether the active provider/model accepts audio inputs. None means unknown.",
    )
    supports_file_input: Optional[bool] = Field(
        default=LLM_DEFAULTS.supports_file_input.value,
        description="Whether the active provider/model accepts native file/document inputs. None means unknown.",
    )
    model_capabilities: Dict[str, Any] = Field(
        default_factory=lambda: copy.deepcopy(LLM_DEFAULTS.model_capabilities.value),
        description="Provider/model-specific input capability facts keyed by provider and model id.",
    )
    default_tools: Tuple[str, ...] = Field(
        default=(),
        description=(
            "Catalog tool ids the agent's definition names; they grant nothing, and groups that load on "
            "request stay shown for it when named here (custom_agents.tool_loading)."
        ),
    )
    reasoning_trace_retention_messages: Optional[int] = Field(
        default=LLM_DEFAULTS.reasoning_trace_retention_messages.value,
        description=(
            "Number of later Strands messages to keep full provider reasoning traces for tool-call "
            "continuation. None disables pruning."
        ),
    )
    subtask_timeout_seconds: Optional[float] = Field(
        default=AGENT_DEFAULTS.subtask_timeout_seconds.value,
        description="Maximum time a delegated subtask may wait for a terminal reply before it is failed locally.",
    )
    subtask_watchdog_interval_seconds: float = Field(
        default=AGENT_DEFAULTS.subtask_watchdog_interval_seconds.value,
        description="Polling interval for overdue delegated-subtask recovery.",
    )
    subtask_status_probe_timeout_seconds: float = Field(
        default=AGENT_DEFAULTS.subtask_status_probe_timeout_seconds.value,
        description="Seconds to wait for an operational TaskStatus response before failing an overdue subtask.",
    )
    restart_executing_task_grace_seconds: Optional[float] = Field(
        default=AGENT_DEFAULTS.restart_executing_task_grace_seconds.value,
        description=(
            "Maximum age for an executing runtime snapshot to be retried after restart. "
            "Older in-flight executions are failed during recovery to avoid infinite restart loops. "
            "Set null or <=0 to always retry interrupted executions."
        ),
    )
    same_role_runtime_takeover_enabled: bool = Field(
        default=AGENT_DEFAULTS.same_role_runtime_takeover_enabled.value,
        description=(
            "Allow this agent to claim persisted active/deferred runtime state owned by another "
            "agent ID with the same team_id and named_agent_type. Enabled by default as the "
            "allowed shared-state exception for restart/failover recovery."
        ),
    )
    max_tool_calls_per_task: Optional[int] = Field(
        default=AGENT_DEFAULTS.max_tool_calls_per_task.value,
        description=(
            "Maximum cumulative tool calls across one local task-processing invocation; the "
            "counter does not reset on each model turn. "
            "When exceeded, the runtime records a summary and fails the task to avoid infinite tool loops. "
            "Set null or <=0 to disable the limit."
        ),
    )
    max_tool_result_tokens_per_call: Optional[int] = Field(
        default=AGENT_DEFAULTS.max_tool_result_tokens_per_call.value,
        description=(
            "Maximum estimated tokens allowed in one tool result before it is replaced with a compact warning. "
            "Set null or <=0 to disable the per-call limit."
        ),
    )
    runtime_budget_policy: Dict[str, Any] = Field(
        default_factory=lambda: copy.deepcopy(
            AGENT_DEFAULTS.runtime_budget_policy.value
        ),
        description="Recoverable runtime budget policy for hard-stop or user-guided pause behavior.",
    )
    turn_action_outbox: Dict[str, Any] = Field(
        default_factory=lambda: copy.deepcopy(AGENT_DEFAULTS.turn_action_outbox.value),
        description="Runtime action outbox retry and recovery policy.",
    )
    runtime_coordinator: Dict[str, Any] = Field(
        default_factory=lambda: copy.deepcopy(AGENT_DEFAULTS.runtime_coordinator.value),
        description="Runtime coordinator policy for task-tree leases, mailbox deferral, and state projection.",
    )
    mid_turn_steering: Dict[str, Any] = Field(
        default_factory=lambda: copy.deepcopy(AGENT_DEFAULTS.mid_turn_steering.value),
        description="Runtime policy for Strands mid-turn interrupts and proactive requester steering.",
    )
    inner_loop_context: Dict[str, Any] = Field(
        default_factory=lambda: merge_inner_loop_context_config(
            AGENT_DEFAULTS.inner_loop_context.value
        ),
        description="Inner-loop Strands transcript filtering and token-budget settings.",
    )
    context_engine: Dict[str, Any] = Field(
        default_factory=lambda: merge_context_engine_config(
            AGENT_DEFAULTS.context_engine.value
        ),
        description="Outer-loop context assembly, budget enforcement, and conversation-thread rendering settings.",
    )
    debug: Dict[str, Any] = Field(
        default_factory=lambda: copy.deepcopy(AGENT_DEFAULTS.debug.value),
        description="Opt-in debugging artifacts for model-visible context and observable runtime traces.",
    )
    structured_output_failure_threshold: Optional[int] = Field(
        default=AGENT_DEFAULTS.structured_output_failure_threshold.value,
        description=(
            "Consecutive failed structured-output validation tool results before stopping the turn."
        ),
    )
    deferred_message_warning_threshold: int = Field(
        default=AGENT_DEFAULTS.deferred_message_warning_threshold.value,
        description="Warn when the deferred inbound message backlog reaches this many envelopes. Set to 0 to disable.",
    )
    max_deferred_messages: int = Field(
        default=AGENT_DEFAULTS.max_deferred_messages.value, ge=1
    )
    context_window_tokens: Optional[int] = Field(
        default=LLM_DEFAULTS.context_window_tokens.value,
        description="Optional explicit context window token limit used for conversation compaction.",
    )
    compaction_model_provider: Optional[str] = Field(
        default=LLM_DEFAULTS.compaction_model_provider.value,
        description="Optional provider override for conversation compaction.",
    )
    compaction_model_id: Optional[str] = Field(
        default=LLM_DEFAULTS.compaction_model_id.value,
        description="Optional model override for conversation compaction.",
    )
    llm_failover: Dict[str, Any] = Field(
        default_factory=lambda: copy.deepcopy(LLM_DEFAULTS.failover.value),
        description="Model-provider failover policy for throttled or overloaded model calls.",
    )
    conversation_compactor_type: str = Field(
        default=CONVERSATION_COMPACTION_DEFAULTS.compactor_type.value,
        description="Strategy type used for durable conversation compaction.",
    )
    conversation_summary_generator_type: str = Field(
        default=AGENT_DEFAULTS.conversation_summary_generator_type.value,
        description="Strategy type used for durable per-turn conversation summaries.",
    )

    # Model keys are not fields: custom_agents.model_keys looks them up per call.

    # LiteLLM Proxy configuration
    litellm_api_base: Optional[str] = Field(
        default=LLM_DEFAULTS.litellm_api_base.value,
        description=(
            "OpenAI API-compatible base URL used by the litellm provider path "
            "(for example, OpenRouter or a LiteLLM proxy)."
        ),
    )
    openai_codex: Dict[str, Any] = Field(
        default_factory=lambda: copy.deepcopy(LLM_DEFAULTS.openai_codex.value),
        description="OpenAI Codex OAuth provider settings.",
    )
    xai_oauth: Dict[str, Any] = Field(
        default_factory=lambda: copy.deepcopy(LLM_DEFAULTS.xai_oauth.value),
        description="xAI Grok OAuth provider settings.",
    )
    agent_result_transformer_spec: Optional[str] = Field(
        default_factory=lambda: os.getenv("AGENT_RESULT_TRANSFORMER"),
        description="Optional module:function hook that can post-process an AgentResult before parsing.",
    )
    context_compaction_trigger_ratio: float = Field(
        default=CONVERSATION_COMPACTION_DEFAULTS.trigger_ratio.value,
        description="Compaction trigger threshold as a fraction of the context window.",
    )
    context_compaction_target_ratio: float = Field(
        default=CONVERSATION_COMPACTION_DEFAULTS.target_ratio.value,
        description="Compaction target threshold as a fraction of the context window.",
    )
    context_compaction_min_recent_turns: int = Field(
        default=CONVERSATION_COMPACTION_DEFAULTS.min_recent_turns.value,
        description="Minimum number of recent raw conversation turns to preserve.",
    )
    context_compaction_max_recent_turns: int = Field(
        default=CONVERSATION_COMPACTION_DEFAULTS.max_recent_turns.value,
        description="Maximum number of recent raw turns to preserve when compaction is active.",
    )
    context_compaction_trigger_turn_count: Optional[int] = Field(
        default=None,
        description="Optional turn-count trigger for conversation compaction.",
    )
    context_compaction_trigger_message_count: Optional[int] = Field(
        default=None,
        description="Optional message-count trigger for conversation compaction.",
    )
    activity_collector_base_url: Optional[str] = Field(
        default=ACTIVITY_COLLECTOR_DEFAULTS.base_url.value,
        description="Optional activity collector base URL for best-effort runtime activity submission.",
    )
    activity_collector_connect_timeout_seconds: float = Field(
        default=ACTIVITY_COLLECTOR_DEFAULTS.connect_timeout_seconds.value,
        description="HTTP connect timeout for the activity collector client.",
    )
    activity_collector_request_timeout_seconds: float = Field(
        default=ACTIVITY_COLLECTOR_DEFAULTS.request_timeout_seconds.value,
        description="HTTP request timeout for the activity collector client.",
    )
    activity_collector_batch_max_events: int = Field(
        default=ACTIVITY_COLLECTOR_DEFAULTS.batch_max_events.value,
        description="Maximum number of activity events per collector batch.",
    )
    activity_collector_flush_interval_seconds: float = Field(
        default=ACTIVITY_COLLECTOR_DEFAULTS.flush_interval_seconds.value,
        description="Periodic flush interval for buffered activity events.",
    )
    activity_collector_max_buffered_events: int = Field(
        default=ACTIVITY_COLLECTOR_DEFAULTS.max_buffered_events.value,
        description="Maximum buffered activity events before new events are dropped.",
    )
    activity_collector_drop_when_unavailable: bool = Field(
        default=ACTIVITY_COLLECTOR_DEFAULTS.drop_when_unavailable.value,
        description="Whether activity events should be dropped when the collector is unavailable.",
    )
    activity_collector_ingest_token_env_var: str = Field(
        default=ACTIVITY_COLLECTOR_DEFAULTS.ingest_token_env_var.value,
        description="Environment variable containing the activity collector ingest token.",
    )
    otel_enabled: bool = Field(
        default=OTEL_DEFAULTS.enabled.value,
        description="Enable OpenTelemetry export through Strands and OTLP.",
    )
    otel_enable_traces: bool = Field(
        default=OTEL_DEFAULTS.enable_traces.value,
        description="Enable OTLP trace export when OTEL is enabled.",
    )
    otel_enable_metrics: bool = Field(
        default=OTEL_DEFAULTS.enable_metrics.value,
        description="Enable OTLP metrics export when OTEL is enabled.",
    )
    otel_otlp_endpoint: Optional[str] = Field(
        default=OTEL_DEFAULTS.otlp_endpoint.value,
        description="OTLP endpoint used when OTEL export is enabled.",
    )
    otel_service_name: Optional[str] = Field(
        default=OTEL_DEFAULTS.service_name.value,
        description="Optional OTEL service name override.",
    )
    otel_headers: Dict[str, str] = Field(
        default_factory=lambda: copy.deepcopy(OTEL_DEFAULTS.headers.value),
        description="Optional OTLP exporter headers.",
    )

    # Private attributes (not serialized by Pydantic)
    _logger: logging.Logger = PrivateAttr(default=None)
    _should_exit: asyncio.Event = PrivateAttr(default_factory=asyncio.Event)
    _agent_state_store: Optional[AgentStateStore] = PrivateAttr(default=None)
    _schedule_store: Optional[ScheduleStore] = PrivateAttr(default=None)
    _scheduler_task: Optional[asyncio.Task] = PrivateAttr(default=None)
    _local_schedule_task: Optional[asyncio.Task] = PrivateAttr(default=None)
    _worker_launch: Any = PrivateAttr(default=None)
    _worker_ready: bool = PrivateAttr(default=False)
    _presence_task: Optional[asyncio.Task] = PrivateAttr(default=None)
    _liveness_observation: LivenessObservation = PrivateAttr(default_factory=LivenessObservation)
    _scheduler_wake: Optional[asyncio.Event] = PrivateAttr(default=None)
    _agent_id_by_name: Dict[str, str] = PrivateAttr(default_factory=dict)
    _strands_agent: Optional[Agent] = PrivateAttr(default=None)
    _strands_model: Optional[Any] = PrivateAttr(default=None)
    # A person's own model for the current turn (custom_agents.person_model_runtime).
    _person_models: Any = PrivateAttr(default=None)
    _runtime_guardrails_plugin: Optional[RuntimeGuardrailsPlugin] = PrivateAttr(
        default=None
    )
    _tool_result_budget_monitor: Optional[TaskAwareToolResultOffloader] = PrivateAttr(
        default=None
    )
    _conversation_history: List[Dict[str, str]] = PrivateAttr(default_factory=list)
    _deferred_inbox: DeferredMessageInbox | None = PrivateAttr(default=None)
    _agent_result_transformer: Optional[Callable[[Any, Any, Any], Any]] = PrivateAttr(
        default=None
    )
    _conversation_store: Optional[ConversationStore] = PrivateAttr(default=None)
    _conversation_compactor: Optional[ConversationCompactor] = PrivateAttr(default=None)
    _conversation_artifact_offloader: Optional[Any] = PrivateAttr(default=None)
    _conversation_summary_generator: Optional[ConversationSummaryGenerator] = (
        PrivateAttr(default=None)
    )
    _context_engine: Optional[ContextEngine] = PrivateAttr(default=None)
    _llm_turn_executor: Optional[llm_turn_runtime.LLMTurnExecutor] = PrivateAttr(
        default=None
    )
    _context_debug_dump_plugin: Optional[ContextDebugDumpPlugin] = PrivateAttr(
        default=None
    )
    _memory_review_store: Optional[MemoryReviewStore] = PrivateAttr(default=None)
    _memory_graph_repository: Optional[Any] = PrivateAttr(default=None)
    _pinned_memory_repository: Optional[Any] = PrivateAttr(default=None)
    _knowledge_repository: Optional[Any] = PrivateAttr(default=None)
    _memory_graph_available: bool = PrivateAttr(default=False)
    _memory_graph_degraded_reason: Optional[str] = PrivateAttr(default=None)
    _embedding_client: Optional[EmbeddingServiceClient] = PrivateAttr(default=None)
    _skills_fingerprint: Optional[tuple[tuple[str, int, int], ...]] = PrivateAttr(
        default=None
    )
    _skill_catalog_runtime_state: SkillCatalogRuntimeState = PrivateAttr(
        default_factory=SkillCatalogRuntimeState
    )
    _started_at: datetime = PrivateAttr(
        default_factory=lambda: datetime.now(timezone.utc)
    )
    _configured_model_provider: Optional[str] = PrivateAttr(default=None)
    _configured_model_id: Optional[str] = PrivateAttr(default=None)
    _execution_gate: AgentExecutionGate = PrivateAttr(default_factory=AgentExecutionGate)
    _active_execution_task: Optional[asyncio.Task] = PrivateAttr(default=None)
    _pending_work_task: Optional[asyncio.Task] = PrivateAttr(default=None)
    # A wake-up arrived that the drain has not yet answered with a pass.
    _pending_work_requested: bool = PrivateAttr(default=False)
    # Failed passes since the last finished one, and the backoff retry timer.
    _pending_work_failures: int = PrivateAttr(default=0)
    _pending_work_retry: Optional[asyncio.TimerHandle] = PrivateAttr(default=None)
    # Timers that wake the drain later, and short background work such as
    # sends; shutdown cancels and joins both (pending_work_runtime).
    _pending_work_timers: set[asyncio.TimerHandle] = PrivateAttr(default_factory=set)
    _owned_tasks: set[asyncio.Task[None]] = PrivateAttr(default_factory=set)
    # Next time to look for decided access requests whose notice never arrived
    # (access_request_runtime.resume_decided_access_waits).
    _decided_access_check_after: float = PrivateAttr(default=0.0)
    # Next time the lead looks for dead agents holding unfinished work
    # (orphaned_work_takeover.take_over_orphaned_work).
    _orphaned_work_check_after: float = PrivateAttr(default=0.0)
    # Next time the lead removes status rows of agents that are gone
    # (agent_liveness.prune_departed_agent_rows).
    _agent_row_prune_after: float = PrivateAttr(default=0.0)
    # Saved work this agent released after another agent took it over
    # (custom_agents.custody_loss); set when the agent connects.
    _custody_release: Any = PrivateAttr(default=None)
    # This agent's last liveness decision per purpose and owner, so each change
    # is logged once, and senders whose clocks run ahead (agent_liveness).
    _liveness_decisions: Dict[Any, Any] = PrivateAttr(default_factory=dict)
    _status_clock_ahead: set[str] = PrivateAttr(default_factory=set)
    _active_execution_task_id: Optional[str] = PrivateAttr(default=None)
    # The running turn's stop for its tool commands (custom_tools.tool_stop).
    _active_tool_stop: Optional[ToolStop] = PrivateAttr(default=None)
    _stop_reasons_by_task_id: Dict[str, str] = PrivateAttr(default_factory=dict)
    # Set once this worker loses its launch lease; see lease_loss_wind_down.
    _lease_loss_wind_down: Any = PrivateAttr(default=None)
    # Set by the worker pool controller when the pool is above its limit: the
    # worker stops asking for work and exits once it holds none.
    _pool_draining: bool = PrivateAttr(default=False)
    # Worker queues the pool controller made sure exist, and its latest view of
    # every pool (custom_agents.worker_pool_controller; read by inspect_worker_pools).
    _ensured_worker_queues: set[str] = PrivateAttr(default_factory=set)
    _worker_pool_snapshot: Dict[str, Any] = PrivateAttr(default_factory=dict)
    # While a model provider refuses calls, the worker holds its task, takes no
    # new work and retries after this monotonic time (custom_agents.provider_limits).
    _provider_cooldown_until: Optional[float] = PrivateAttr(default=None)
    _provider_cooldown: Any = PrivateAttr(default=None)
    _activity_client: Optional[ActivityCollectorClient] = PrivateAttr(default=None)
    _operational_exchange_name: Optional[str] = PrivateAttr(default=None)
    _operational_queue_name: Optional[str] = PrivateAttr(default=None)
    # Probes sent to a worker type (no known holder): only a reply from a worker
    # that knows the task counts (subtask_watchdog_runtime).
    _known_only_task_status_probes: set[str] = PrivateAttr(default_factory=set)
    _untracked_subtask_warnings: set[str] = PrivateAttr(default_factory=set)
    _pending_task_status_futures: Dict[str, asyncio.Future] = PrivateAttr(
        default_factory=dict
    )
    _pending_agent_status_futures: Dict[
        str,
        tuple[Optional[str], Optional[str], asyncio.Future],
    ] = PrivateAttr(default_factory=dict)
    _runtime_loop: Optional[asyncio.AbstractEventLoop] = PrivateAttr(default=None)
    _task_runtime_coordinator: Optional[TaskRuntimeCoordinator] = PrivateAttr(
        default=None
    )
    _runtime_steering_plugin: Optional[RuntimeSteeringPlugin] = PrivateAttr(
        default=None
    )
    _pending_proactive_task_steering: Dict[str, List[TaskSteering]] = PrivateAttr(
        default_factory=dict
    )
    _pending_interrupt_responses: Dict[str, TaskSteering] = PrivateAttr(
        default_factory=dict
    )
    _interrupted_turn_sessions: Dict[str, StrandsInterruptedTurnSession] = PrivateAttr(
        default_factory=dict
    )
    _processed_task_steering_ids: set[str] = PrivateAttr(default_factory=set)
    _runtime_repair_anomaly_counts: Dict[str, int] = PrivateAttr(default_factory=dict)
    _runtime_repair_anomalies: List[Dict[str, Any]] = PrivateAttr(default_factory=list)
    _runtime_repair_anomaly_limit: int = PrivateAttr(default=50)
    _local_team_runtime: LocalAgentTeamRuntime | None = PrivateAttr(default=None)

    model_config = ConfigDict(arbitrary_types_allowed=True)

    @property
    def is_executing(self) -> bool:
        """Execution availability is derived from the current owner's lifetime."""
        return self._execution_gate.busy

    @contextlib.asynccontextmanager
    async def _execution_scope(self) -> AsyncIterator[None]:
        nested = self._execution_gate.owned_by_current_task
        if self.is_executing and not nested:
            self._logger.info(
                "Waiting for the active agent execution to finish. "
                "event=agent_execution_waiting agent_id=%s active_task_id=%s",
                self.id,
                self._active_execution_task_id,
            )
        try:
            async with self._execution_gate.hold():
                if not nested:
                    await self._announce_execution_state()
                yield
        finally:
            if not nested:
                await self._announce_execution_state()

    async def _announce_execution_state(self) -> None:
        """Record and broadcast that this agent started or finished work.

        The heartbeat alone reports a change up to NATS_PRESENCE_HEARTBEAT_SECONDS
        late; Web Chat's activity mesh lights an agent while it works, so the
        change is broadcast as it happens. A failed broadcast never affects the
        work; the next heartbeat repeats the state.
        """

        await self._refresh_self_agent_state()
        try:
            await self._publish_presence_status(is_exiting=False)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self._logger.warning(
                "Could not broadcast the agent's working state; the next heartbeat repeats it. "
                "event=agent_execution_presence_failed agent_id=%s is_executing=%s error_type=%s",
                self.id,
                self.is_executing,
                type(exc).__name__,
            )

    def __init__(self, **kwargs):
        """
        Initializes the UniversalAgent.

        Sets up agent-specific queue names and populates the map of queues
        for different named agent types, which are used for task submission.
        Creates the internal Strands Agent with the configured system prompt and tools.
        """
        local_team_runtime = kwargs.pop("local_team_runtime", None)
        super().__init__(**kwargs)
        self._local_team_runtime = local_team_runtime
        # Work saved or freed while another holder had the slot gets a drain
        # pass when that holder lets go (pending_work_runtime).
        self._execution_gate.on_release = partial(wake_after_slot_release, self)

        self._configured_model_provider = self.model_provider
        self._configured_model_id = self.model_id

        # add last 4 digits of ID to the name for easier visual identification
        self.name = f"{self.name}-{self.id[-4:]}"

        # Initialize logger
        self._logger = logging.getLogger(f"UniversalAgent.{self.id}")
        self._logger.info(f"[{self.id}] Agent '{self.name}' created with ID: {self.id}")
        fill_missing_instance_paths(self)
        configure_strands_otel(
            enabled=self.otel_enabled,
            enable_traces=self.otel_enable_traces,
            enable_metrics=self.otel_enable_metrics,
            otlp_endpoint=self.otel_otlp_endpoint,
            service_name=self.otel_service_name,
            headers=self.otel_headers,
            logger=self._logger,
        )
        self._agent_result_transformer = load_agent_result_transformer(
            self.agent_result_transformer_spec
        )
        embedding_settings_payload = {
            "provider": self.embedding_provider
            if self.memory_graph_enabled
            else "local",
            "model_id": self.embedding_model_id,
            "api_base": self.embedding_api_base,
            "dimensions": self.embedding_dimensions,
            "timeout_seconds": self.embedding_timeout_seconds,
            "organization": self.embedding_organization,
            "extra_headers": dict(self.embedding_extra_headers or {}),
        }
        embedding_settings = create_embedding_service_settings(embedding_settings_payload)
        self._embedding_client = EmbeddingServiceClient(
            embedding_settings,
            key=embedding_endpoint_key(embedding_settings, chat_api_base=self.litellm_api_base),
            logger=self._logger,
        )
        self._activity_client = ActivityCollectorClient(
            ActivityCollectorClientSettings(
                base_url=self.activity_collector_base_url,
                connect_timeout_seconds=self.activity_collector_connect_timeout_seconds,
                request_timeout_seconds=self.activity_collector_request_timeout_seconds,
                batch_max_events=self.activity_collector_batch_max_events,
                flush_interval_seconds=self.activity_collector_flush_interval_seconds,
                max_buffered_events=self.activity_collector_max_buffered_events,
                drop_when_unavailable=self.activity_collector_drop_when_unavailable,
                ingest_token_env_var=self.activity_collector_ingest_token_env_var,
            ),
            logger=self._logger,
        )
        self.task_manager.set_owner_identity(
            self.id, self.name, self.team_id, self.named_agent_type
        )
        self.task_manager.set_activity_emitter(self.emit_activity_event)
        shared_storage_settings = self.task_manager.storage_settings
        self._conversation_store = ConversationStore(
            db_path=self.task_manager._db_path,
            artifacts_dir=agent_layout(self).conversation_artifacts_dir,
            logger=self._logger,
            storage_settings=shared_storage_settings,
        )
        self._memory_review_store = MemoryReviewStore(
            db_path=self.task_manager._db_path,
            logger=self._logger,
            storage_settings=shared_storage_settings,
        )
        self._pinned_memory_repository = create_pinned_memory_repository(
            shared_storage_settings,
            logger=self._logger,
        )
        # The knowledge base shares the manager database with pinned memory;
        # with agent.knowledge.enabled false no store is opened and the tools
        # say the knowledge base is not available.
        if knowledge_settings(self.knowledge_config).enabled:
            self._knowledge_repository = create_knowledge_repository(
                shared_storage_settings,
                logger=self._logger,
            )
        if self.memory_graph_enabled:
            self._memory_graph_repository = create_memory_graph_repository(
                shared_storage_settings,
                logger=self._logger,
            )
            ensure_memory_graph_embedding_profile(
                repository=self._memory_graph_repository,
                profile_spec=build_embedding_profile_spec(embedding_settings_payload),
                logger=self._logger,
            )
            self._probe_memory_graph_embedding_backend(embedding_settings_payload)
            if self._memory_graph_available:
                self._logger.info(
                    "[%s] Enabled memory graph retrieval on storage backend %s.",
                    self.id,
                    shared_storage_settings.backend,
                )
        self._conversation_compactor = build_conversation_compactor(
            self.conversation_compactor_type,
            config=ConversationCompactionConfig(
                context_window_tokens=self._resolved_context_window_tokens(),
                trigger_ratio=self.context_compaction_trigger_ratio,
                target_ratio=self.context_compaction_target_ratio,
                min_recent_turns=self.context_compaction_min_recent_turns,
                max_recent_turns=self.context_compaction_max_recent_turns,
                trigger_turn_count=self.context_compaction_trigger_turn_count,
                trigger_message_count=self.context_compaction_trigger_message_count,
                compaction_model_provider=self.compaction_model_provider,
                compaction_model_id=self.compaction_model_id,
                default_model_provider=self.model_provider,
                default_model_id=self.model_id,
                temperature=self.temperature,
                litellm_api_base=self.litellm_api_base,
                litellm_api_mode=self.litellm_api_mode,
                openai_codex=self.openai_codex,
                xai_oauth=self.xai_oauth,
                instance_root=infer_instance_root(self),
            ),
            logger=self._logger,
        )
        self._conversation_artifact_offloader = None
        self._conversation_summary_generator = build_conversation_summary_generator(
            self.conversation_summary_generator_type,
            config=self._conversation_summary_config(),
            logger=self._logger,
        )

        # Build system prompt from role/goal/backstory if not provided directly
        if not self.system_prompt and (self.role or self.goal or self.backstory):
            self.system_prompt = self._build_system_prompt()

        # Inject self into tools that need agent context
        for tool in self.tools:
            if hasattr(tool, "set_agent"):
                tool.set_agent(self)

        # Set up named agent type queue mappings based on team lead status
        if self.is_team_lead:
            self._setup_team_lead_queues()
        else:
            # If not team lead, just listen to the generic queue for own named_agent_type
            if self.named_agent_type:
                self.named_agent_type_queue_map = {
                    self.named_agent_type: f"{self.team_id}_{self.named_agent_type}",
                    TEAM_ARCHITECT_AGENT_TYPE: f"{self.team_id}_{TEAM_ARCHITECT_AGENT_TYPE}",
                }

        self.team_comms.configure_transport(
            team_id=self.team_id,
            named_agent_type_queue_map=self.named_agent_type_queue_map,
            nats_url=self.nats_url,
            nats_servers=self.nats_servers,
            nats_name=self.nats_name,
            nats_jetstream_enabled=self.nats_jetstream_enabled,
            nats_subject_prefix=self.nats_subject_prefix,
            nats_storage=self.nats_storage,
            nats_replicas=self.nats_replicas,
            nats_ack_wait_seconds=self.nats_ack_wait_seconds,
            nats_max_deliver=self.nats_max_deliver,
            nats_duplicate_window_seconds=self.nats_duplicate_window_seconds,
            nats_work_max_age_seconds=self.nats_work_max_age_seconds,
            nats_presence_max_age_seconds=self.nats_presence_max_age_seconds,
            nats_process_consumer_inactive_threshold_seconds=(
                self.nats_process_consumer_inactive_threshold_seconds
            ),
            nats_user=self.nats_user,
            nats_password=self.nats_password,
            nats_credentials_file=self.nats_credentials_file,
        )

        self._strands_model = self._configure_model()
        self._create_context_engine()

        # Create the internal Strands Agent
        self._create_strands_agent()

    def _create_context_engine(self) -> None:
        """Create the default context engine for outer-loop prompt assembly."""
        context_engine_config = merge_context_engine_config(self.context_engine)
        context_engine_type = (
            str(context_engine_config.get("type") or "default").strip().lower()
        )
        if context_engine_type != "default":
            raise ValueError(
                f"Unsupported context engine type: {context_engine_type!r}"
            )
        context_plugins = build_context_engine_plugins(
            list(context_engine_config.get("plugins") or []),
            diagnostics_dir=agent_layout(self).diagnostics_dir,
            logger=self._logger,
        )
        self._context_debug_dump_plugin = ContextDebugDumpPlugin(
            config=(self.debug or {}).get("context_dump"),
            output_dir=agent_layout(self).diagnostics_dir / "context_dumps",
            logger=self._logger,
            agent_metadata_provider=self.turn_executor._context_debug_agent_metadata,
            strands_messages_provider=self.turn_executor._context_debug_strands_messages,
            reasoning_artifacts_builder=self.turn_executor._context_debug_reasoning_artifacts,
            task_history_summary_provider=self.turn_executor._context_debug_task_history_summary,
        )
        context_plugins.append(self._context_debug_dump_plugin)

        # Personal memory and the working-folder line follow the task's verified
        # person, as its tools do (access_runtime.verified_meta_person_id).
        def verified_person_of(meta: Any) -> Optional[str]:
            return verified_meta_person_id(self, meta)

        memory_plugin_manager = None
        if bool(
            ((self.memory_config or {}).get("plugin_manager") or {}).get(
                "enabled", True
            )
        ):
            memory_plugin_manager = MemoryPluginManager(
                agent_id=self.id,
                team_id=self.team_id,
                named_agent_type=self.named_agent_type,
                verified_person_of=verified_person_of,
                config=dict(self.memory_config or {}),
                logger=self._logger,
                memory_review_store=self._memory_review_store,
                memory_graph_repository=self._memory_graph_repository
                if self._memory_graph_available
                else None,
                pinned_memory_repository=self._pinned_memory_repository,
                knowledge_repository=self._knowledge_repository,
                conversation_store=self._conversation_store,
                embed_text_fn=self._embed_text
                if self._memory_graph_available
                else None,
                review_job_sink=self._dispatch_memory_review_job,
            )
        self._context_engine = DefaultContextEngine(
            agent_id=self.id,
            logger=self._logger,
            task_manager=self.task_manager,
            conversation_store=self._conversation_store,
            conversation_compactor=self._conversation_compactor,
            memory_graph_repository=self._memory_graph_repository
            if self._memory_graph_available
            else None,
            system_prompt=self.system_prompt,
            context_window_tokens=self._resolved_context_window_tokens(),
            model=self._strands_model,
            **context_engine_kwargs_from_config(context_engine_config),
            plugins=context_plugins,
            inner_loop_context=self.inner_loop_context,
            workspace_dir=self.workspace_dir or ".",
            instance_root=self.instance_root,
            is_team_lead=self.is_team_lead,
            instance_config_path=self.instance_config_path,
            memory_plugin_manager=memory_plugin_manager,
            adapters=ContextEngineAdapters(
                build_request_time_context=lambda meta: build_request_time_context(
                    meta,
                    logger=self._logger,
                    agent_id=self.id,
                ),
                build_request_conversation_context=lambda meta, request_text: (
                    build_request_conversation_context(
                        meta,
                        request_text=request_text,
                    )
                ),
                build_team_routing_prompt_context=self._build_team_routing_prompt_context,
                verified_person_of=verified_person_of,
                build_tool_setup_context=lambda meta: tool_setup_runtime.tool_setup_context(self, meta),
                build_person_role_context=lambda meta: person_role_context(self, meta),
            ),
        )

    @staticmethod
    def _provider_overload_error_summary(
        error: BaseException, *, max_chars: int = 900
    ) -> str:
        return llm_turn_runtime._provider_overload_error_summary(
            error, max_chars=max_chars
        )

    def _build_system_prompt(self) -> str:
        """Builds the system prompt from role, goal, and backstory components.

        Performs template substitution for placeholders like:
        - {{team_id_undefined}} -> team_id
        """
        prompt_parts = []
        if self.role:
            prompt_parts.append(f"Your Role: {self.role}")
        if self.goal:
            prompt_parts.append(f"Your Goal: {self.goal}")
        if self.backstory:
            # Perform template substitution
            filled_backstory = self.backstory.replace(
                MESSAGE_FILL_NAME.TEAM_ID, self.team_id
            )
            prompt_parts.append(f"Team Backstory: {filled_backstory}")
        return "\n\n".join(prompt_parts)

    def _format_uptime(self) -> str:
        """Return human-readable uptime since this agent instance started."""
        elapsed = max(
            0, int((datetime.now(timezone.utc) - self._started_at).total_seconds())
        )
        days, remainder = divmod(elapsed, 86400)
        hours, remainder = divmod(remainder, 3600)
        minutes, seconds = divmod(remainder, 60)
        return f"{days}d {hours}h {minutes}m {seconds}s"

    def _resolve_manager_identity(self, *args, **kwargs):
        return delegation_runtime._resolve_manager_identity(self, *args, **kwargs)

    def _agent_status_matches_named_type(self, *args, **kwargs):
        return delegation_runtime._agent_status_matches_named_type(
            self, *args, **kwargs
        )

    def _resolve_pending_agent_status_probes(self, *args, **kwargs):
        return delegation_runtime._resolve_pending_agent_status_probes(
            self, *args, **kwargs
        )

    async def _record_delegation_subtask_abort(self, *args, **kwargs):
        return await delegation_runtime._record_delegation_subtask_abort(
            self, *args, **kwargs
        )

    async def _probe_live_named_agent_type(self, *args, **kwargs):
        return await delegation_runtime._probe_live_named_agent_type(
            self, *args, **kwargs
        )

    async def _verify_delegation_target(self, *args: Any, **kwargs: Any) -> bool:
        return await delegation_runtime._verify_delegation_target(self, *args, **kwargs)

    async def _create_subtask_request(self, *args, **kwargs):
        return await delegation_runtime._create_subtask_request(self, *args, **kwargs)

    @staticmethod
    def _task_request_transport_copy(*args, **kwargs):
        return delegation_runtime._task_request_transport_copy(*args, **kwargs)

    @staticmethod
    def _task_response_history_transport_copy(*args, **kwargs):
        return delegation_runtime._task_response_history_transport_copy(*args, **kwargs)

    def emit_activity_event(
        self,
        *,
        activity_type: TaskActivityType,
        stage: TaskActivityStage,
        summary: str,
        detail: Optional[str] = None,
        task_id: Optional[str] = None,
        parent_task_id: Optional[str] = None,
        top_level_task_id: Optional[str] = None,
        objective_id: Optional[str] = None,
        objective_title: Optional[str] = None,
        meta: Optional[MessageMeta] = None,
        user_visible: bool = True,
        details: Optional[Dict[str, Any]] = None,
        created_at: Optional[str] = None,
    ) -> None:
        if not self._activity_client or not self._activity_client.enabled:
            return

        effective_task_id = task_id or parent_task_id
        task_info = (
            self.task_manager.get_task_info(effective_task_id)
            if effective_task_id
            else None
        )
        parent_info = (
            self.task_manager.get_task_info(parent_task_id) if parent_task_id else None
        )
        effective_meta = (
            meta
            or (task_info.meta if task_info else None)
            or (parent_info.meta if parent_info else None)
        )
        effective_parent_task_id = (
            parent_task_id
            or (task_info.task.parent_task_id if task_info else None)
            or (parent_info.task.parent_task_id if parent_info else None)
        )
        effective_top_level_task_id = (
            top_level_task_id
            or self.task_manager._get_top_level_task_id(
                effective_task_id or effective_parent_task_id
            )
        )
        gateway_context = (
            getattr(effective_meta, "gateway_context", None) if effective_meta else None
        )
        manager_agent_id, manager_name = self._resolve_manager_identity()
        trace_id, span_id = current_trace_context()

        event = TaskActivityEvent(
            created_at=created_at or datetime.now(timezone.utc).isoformat(),
            sequence=self._activity_client.next_sequence(),
            team_id=self.team_id,
            manager_agent_id=manager_agent_id,
            manager_name=manager_name,
            top_level_task_id=effective_top_level_task_id,
            task_id=task_id,
            parent_task_id=effective_parent_task_id,
            objective_id=objective_id,
            objective_title=objective_title,
            correlation_id=getattr(effective_meta, "msg_correlation_id", None)
            if effective_meta
            else None,
            conversation_id=getattr(effective_meta, "conversation_id", None)
            if effective_meta
            else None,
            session_key=getattr(gateway_context, "session_key", None),
            agent_id=self.id,
            agent_name=self.name,
            named_agent_type=self.named_agent_type,
            activity_type=activity_type,
            stage=stage,
            summary=summary,
            detail=detail,
            user_visible=user_visible,
            trace_id=trace_id,
            span_id=span_id,
            details=details or {},
        )
        self._activity_client.emit(event)

    def _mark_memory_graph_degraded(
        self, reason: str, *, embedding_settings: Optional[Dict[str, Any]] = None
    ) -> None:
        self._memory_graph_available = False
        self._memory_graph_degraded_reason = reason
        provider = str(
            (embedding_settings or {}).get("provider") or self.embedding_provider or ""
        )
        model_id = str(
            (embedding_settings or {}).get("model_id") or self.embedding_model_id or ""
        )
        api_base = str(
            (embedding_settings or {}).get("api_base") or self.embedding_api_base or ""
        )
        self._logger.error(
            "[%s] Memory graph retrieval disabled for this agent run because embedding health check failed: "
            "provider=%s model_id=%s api_base=%s reason=%s",
            self.id,
            provider or "unknown",
            model_id or "unset",
            api_base or "unset",
            reason,
        )
        self.emit_activity_event(
            activity_type=TaskActivityType.FAILED,
            stage=TaskActivityStage.FAILED,
            summary="Memory graph retrieval disabled.",
            detail=reason,
            user_visible=False,
            details={
                "component": "memory_graph_embedding",
                "provider": provider or None,
                "model_id": model_id or None,
                "api_base": api_base or None,
            },
        )

    def _probe_memory_graph_embedding_backend(
        self, embedding_settings: Dict[str, Any]
    ) -> None:
        if self._embedding_client is None:
            self._mark_memory_graph_degraded(
                "Embedding client is not initialized.",
                embedding_settings=embedding_settings,
            )
            return
        try:
            self._embedding_client.embed_text("memory graph embedding health check")
        except Exception as exc:
            self._mark_memory_graph_degraded(
                str(exc), embedding_settings=embedding_settings
            )
            return
        self._memory_graph_available = True
        self._memory_graph_degraded_reason = None

    def _resolved_context_window_tokens(self) -> int:
        return resolve_context_window_tokens(
            provider=self.model_provider,
            model_id=self.model_id,
            explicit_context_window_tokens=self.context_window_tokens,
        )

    def _parse_iso_timestamp(self, timestamp: Optional[str]) -> Optional[datetime]:
        if not timestamp:
            return None
        try:
            parsed = datetime.fromisoformat(timestamp)
        except (TypeError, ValueError):
            return None
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed

    def _has_execution_capacity(self) -> bool:
        return (
            not self.paused
            and not self.is_executing
            and self.task_manager.can_start_new_task()
        )

    def _recovery_retry_deadline_seconds(self) -> Optional[float]:
        raw = self.restart_executing_task_grace_seconds
        if raw is None:
            return None
        try:
            value = float(raw)
        except (TypeError, ValueError):
            return None
        if value <= 0:
            return None
        return value

    def _resolved_max_tool_calls_per_task(self) -> Optional[int]:
        raw = self.max_tool_calls_per_task
        if raw is None:
            return None
        try:
            value = int(raw)
        except (TypeError, ValueError):
            return None
        if value <= 0:
            return None
        return value

    def _runtime_budget_policy_config(self) -> Dict[str, Any]:
        return runtime_budget_policy_config(self.runtime_budget_policy)

    def _turn_action_outbox_config(self) -> Dict[str, Any]:
        defaults = copy.deepcopy(AGENT_DEFAULTS.turn_action_outbox.value)
        configured = (
            self.turn_action_outbox if isinstance(self.turn_action_outbox, dict) else {}
        )
        merged = copy.deepcopy(defaults)
        merged.update(configured or {})
        return merged

    def _turn_action_outbox_int(self, key: str, default: int) -> int:
        raw = self._turn_action_outbox_config().get(key, default)
        try:
            return max(0, int(raw))
        except (TypeError, ValueError):
            return default

    def _turn_action_outbox_float(self, key: str, default: float) -> float:
        raw = self._turn_action_outbox_config().get(key, default)
        try:
            return max(0.0, float(raw))
        except (TypeError, ValueError):
            return default

    def _runtime_coordinator(self) -> TaskRuntimeCoordinator:
        if self._task_runtime_coordinator is None:
            self._task_runtime_coordinator = TaskRuntimeCoordinator(
                agent_id=self.id,
                logger=self._logger,
                configuration=lambda: dict(self.runtime_coordinator or {}),
                available=lambda: not self.paused and not self.is_executing,
                runtime=self.task_manager.runtime_operations_coordinator,
                operations_runtime=self.task_manager.operations_runtime,
                get_task_info=self.task_manager.get_task_info,
                is_terminal_state=self.task_manager.is_terminal_state,
                dispatch=RuntimeMailboxDispatch(
                    enqueue_fired_scheduled_task=self._enqueue_fired_scheduled_task,
                    fail_subtask_and_reconcile_parent=self._fail_subtask_and_reconcile_parent,
                    handle_task_response=self._handle_task_response,
                    handle_task_query_message=self._handle_task_query_message,
                ),
            )
        return self._task_runtime_coordinator

    def _runtime_budget_policy_mode(self) -> str:
        return runtime_budget_policy_mode(
            self.runtime_budget_policy, logger=self._logger, agent_id=self.id
        )

    def _runtime_budget_trigger_enabled(self, trigger: RecoverablePauseTrigger) -> bool:
        return runtime_budget_trigger_enabled(self.runtime_budget_policy, trigger)

    def _max_user_guided_pauses_per_task(self) -> int:
        return max_user_guided_pauses_per_task(self.runtime_budget_policy)

    def _partial_tool_call_budget_multiplier(self) -> float:
        return partial_tool_call_budget_multiplier(self.runtime_budget_policy)

    def _partial_tool_call_limit(self, base_limit: Optional[int]) -> Optional[int]:
        return partial_tool_call_limit(self.runtime_budget_policy, base_limit)

    @staticmethod
    def _parse_runtime_pause_decision(
        text: str,
        *,
        allowed_decisions: Optional[List[TaskQueryDecision]] = None,
    ) -> Optional[str]:
        return parse_runtime_pause_decision(text, allowed_decisions=allowed_decisions)

    @staticmethod
    def _runtime_pause_allowed_decisions(
        trigger: Optional[RecoverablePauseTrigger],
    ) -> List[TaskQueryDecision]:
        if trigger == RecoverablePauseTrigger.PROVIDER_OVERLOADED:
            return [
                TaskQueryDecision.RETRY,
                TaskQueryDecision.RETRY_IN_5_MINUTES,
                TaskQueryDecision.RETRY_IN_10_MINUTES,
                TaskQueryDecision.CANCEL,
            ]
        return [
            TaskQueryDecision.RESUME,
            TaskQueryDecision.PARTIAL,
            TaskQueryDecision.CANCEL,
        ]

    def _runtime_pause_prompt(
        self,
        friendly_reason: str,
        *,
        trigger: Optional[RecoverablePauseTrigger] = None,
    ) -> str:
        if trigger == RecoverablePauseTrigger.PROVIDER_OVERLOADED:
            return provider_overload_pause_prompt(friendly_reason)
        return runtime_pause_prompt(friendly_reason)

    def _runtime_resume_directive(self, decision: str, trigger: Optional[str]) -> str:
        if decision == TaskQueryDecision.RETRY.value:
            return (
                "Runtime retry instruction: the requester selected option 1 after a provider overload. "
                "Retry the interrupted model work from the current task state. Do not restart completed work "
                "or repeat completed research unless the current task history shows it is necessary."
            )
        if decision == TaskQueryDecision.PARTIAL.value:
            return (
                "Runtime partial-result instruction: the requester selected option 2 after a runtime budget pause. "
                "Use the current execution digest, task history, subtask results, and recorded failure details to "
                "return the best partial result now. Avoid broad new research or exploratory tool use."
            )
        trigger_text = f" Trigger: {trigger}." if trigger else ""
        return (
            "Runtime continuation instruction: the requester selected option 1 after a runtime budget pause. "
            "Continue from the current execution digest, task history, subtask results, and recorded failure details. "
            "Do not restart the task from scratch or repeat completed research; proceed with the next incomplete step."
            f"{trigger_text}"
        )

    @staticmethod
    def _provider_retry_delay_minutes(decision: str) -> Optional[int]:
        if decision == TaskQueryDecision.RETRY_IN_5_MINUTES.value:
            return 5
        if decision == TaskQueryDecision.RETRY_IN_10_MINUTES.value:
            return 10
        return None

    @staticmethod
    def _recoverable_trigger_from_task_history(
        task: Task,
    ) -> Optional[RecoverablePauseTrigger]:
        for entry in reversed(list(task.execution_history or [])):
            details = entry.details if isinstance(entry.details, dict) else {}
            trigger_value = details.get("trigger")
            if not trigger_value:
                continue
            try:
                return RecoverablePauseTrigger(str(trigger_value))
            except ValueError:
                continue
        return None

    def _find_original_task_query(
        self,
        parent_task_id: str,
        task_query_id: str,
    ) -> Optional[TaskQuery]:
        for query in reversed(self.task_manager.get_task_queries(parent_task_id)):
            if (
                query.task_query_id == task_query_id
                and query.query_type == MessageMetaType.REQUEST
            ):
                return query
        return None

    def _friendly_budget_reason(
        self,
        *,
        trigger: RecoverablePauseTrigger,
        msg_content: ContentType,
        limit_error: Optional[ToolCallLimitExceeded] = None,
        detail: Optional[str] = None,
    ) -> str:
        return friendly_budget_reason(
            trigger=trigger,
            named_agent_type=self.named_agent_type,
            limit_error=limit_error,
            detail=detail,
        )

    async def _record_tool_result_budget_events(
        self, *, msg_content: ContentType, result: AgentResult
    ) -> None:
        state = getattr(result, "state", None)
        if not isinstance(state, dict):
            return
        events = state.get(TaskAwareToolResultOffloader._EVENTS_KEY)
        if not isinstance(events, list) or not events:
            return
        history_task_id = self._history_task_id_for_content(msg_content)
        self._logger.warning(
            "[%s] Tool result budget produced %s oversized-result event(s): task_id=%s events=%s",
            self.id,
            len(events),
            history_task_id or "unknown",
            _compact_debug_preview(events[-5:], max_chars=2000),
        )
        if not history_task_id:
            return
        await self.task_manager.operations_runtime.run(
            self.task_manager.append_history_entry,
            history_task_id,
            (
                f"Replaced {len(events)} oversized tool result(s) with compact offload payloads "
                "to protect the active model context."
            ),
            entry_type=TaskHistoryType.ERROR_EVENT,
            importance=TaskHistoryImportance.HIGH,
            details={
                "reason": "tool_result_budget_exceeded",
                "events": events[-10:],
            },
        )

    async def _record_runtime_repair_anomaly(
        self,
        event: str,
        summary: str,
        *,
        severity: str = "warning",
        related_task_id: Optional[str] = None,
        details: Optional[Dict[str, Any]] = None,
    ) -> None:
        timestamp = datetime.now(timezone.utc).isoformat()
        safe_details = dict(details or {})
        count = self._runtime_repair_anomaly_counts.get(event, 0) + 1
        self._runtime_repair_anomaly_counts[event] = count
        anomaly = {
            "event": event,
            "summary": summary,
            "severity": severity,
            "timestamp": timestamp,
            "related_task_id": related_task_id,
            "count_for_event": count,
            "details": safe_details,
        }
        self._runtime_repair_anomalies.append(anomaly)
        if len(self._runtime_repair_anomalies) > self._runtime_repair_anomaly_limit:
            del self._runtime_repair_anomalies[: -self._runtime_repair_anomaly_limit]

        log_method = self._logger.warning
        if severity == "info":
            log_method = self._logger.info
        elif severity in {"error", "critical"}:
            log_method = self._logger.error
        log_method(
            "[%s] event=runtime_repair_anomaly repair_event=%s severity=%s related_task_id=%s "
            "count=%s summary=%s details=%s",
            self.id,
            event,
            severity,
            related_task_id or "unknown",
            count,
            summary,
            _compact_debug_preview(safe_details, max_chars=1200),
        )

        if not related_task_id:
            return
        try:
            if not await self.task_manager.operations_runtime.run(
                self.task_manager.get_task_info, related_task_id
            ):
                return
            await self.task_manager.operations_runtime.run(
                self.task_manager.append_history_entry,
                related_task_id,
                f"Runtime repair anomaly: {summary}",
                source=TaskHistorySource.SYSTEM,
                entry_type=TaskHistoryType.ERROR_EVENT,
                importance=TaskHistoryImportance.HIGH,
                timestamp=timestamp,
                agent_name=self.name,
                agent_id=self.id,
                details={
                    "event": "runtime_repair_anomaly",
                    "repair_event": event,
                    "severity": severity,
                    "count_for_event": count,
                    **safe_details,
                },
            )
        except Exception as exc:
            self._logger.warning(
                "[%s] event=runtime_repair_history_record_failed repair_event=%s related_task_id=%s error=%s",
                self.id,
                event,
                related_task_id,
                exc,
            )

    def _runtime_repair_anomaly_snapshot(self) -> Dict[str, Any]:
        return {
            "total_count": sum(self._runtime_repair_anomaly_counts.values()),
            "counts_by_event": dict(
                sorted(self._runtime_repair_anomaly_counts.items())
            ),
            "recent": list(self._runtime_repair_anomalies[-10:]),
            "buffered_recent_count": len(self._runtime_repair_anomalies),
            "recent_buffer_limit": self._runtime_repair_anomaly_limit,
        }

    def _history_task_id_for_content(self, msg_content: ContentType) -> Optional[str]:
        if isinstance(msg_content, Task):
            return msg_content.task_id
        if isinstance(msg_content, TaskQuery):
            return msg_content.parent_task_id
        if isinstance(
            msg_content,
            (
                SecureCredentialCollectionRequest,
                SecureCredentialCollectionResult,
                ToolSetupRequestV1,
                ToolSetupResultV1,
                SignupAutomationRequest,
            ),
        ):
            return msg_content.parent_task_id
        if isinstance(msg_content, AfterActionReport):
            return msg_content.parent_task_id or msg_content.source_task_id
        return None

    def _format_tool_count_summary(self, tool_sequence: List[str]) -> str:
        return format_tool_count_summary(tool_sequence)

    def _build_team_routing_prompt_context(self) -> str:
        # Worker types come from the current catalog on every turn, so a type
        # created while the team runs is listed at once.
        if not self.is_team_lead:
            return ""
        worker_types = [
            named_type
            for named_type in _local_named_agent_types(self._local_team_runtime, self.named_agent_type)
            if named_type != self.named_agent_type
        ]
        if not worker_types:
            return ""

        lines = [
            "When delegating a new Task request, set `target_named_agent_type` to one of these exact values.",
            "The task waits in that type's shared queue until a free worker takes it; the runtime starts workers.",
            "To send a task to one specific running worker instead (it needs context only that worker has, or "
            "only it is set up for the work), also set `target_agent_name` to that worker's name: the task goes "
            "to that worker's own mailbox, waits behind its current work, and is refused if it is not running.",
            "Do not leave both `target_named_agent_type` and `target_agent_name` empty on a Task request.",
            "Do not write raw queue names in the structured response; the runtime resolves the queue.",
            "",
            "Available specialist targets:",
        ]
        lines.extend(f"- `{named_type}`" for named_type in sorted(worker_types))
        return "\n".join(lines)

    def _collect_recent_strands_tool_transcript(
        self, *, max_blocks: int = 8
    ) -> List[Dict[str, Any]]:
        strands_agent = getattr(self, "_strands_agent", None)
        return collect_recent_strands_tool_transcript(
            strands_agent, max_blocks=max_blocks
        )

    async def _record_tool_call_limit_exceeded(
        self,
        *,
        msg_content: ContentType,
        limit_error: ToolCallLimitExceeded,
    ) -> None:
        history_task_id = self._history_task_id_for_content(msg_content)
        count_summary = self._format_tool_count_summary(limit_error.tool_sequence)
        recent_tool_transcript = self._collect_recent_strands_tool_transcript()
        self._logger.warning(
            f"[{self.id}] Aborting execution after tool-call limit exceeded: "
            f"task_id={history_task_id or 'unknown'} observed={limit_error.observed_tool_calls} "
            f"limit={limit_error.max_tool_calls} last_tool={limit_error.last_attempted_tool or 'unknown'} "
            f"tool_counts={count_summary} recent_attempted_tools="
            f"{_compact_debug_preview(limit_error.recent_tool_uses[-5:], max_chars=1600)} "
            f"recent_tool_transcript={_compact_debug_preview(recent_tool_transcript, max_chars=2000)}"
        )

        if not history_task_id:
            return

        execution_digest = await self.task_manager.operations_runtime.run(
            self.task_manager.render_execution_digest_markdown, history_task_id
        )
        recent_history = await self.task_manager.operations_runtime.run(
            self.task_manager.build_compact_history, history_task_id, max_entries=8
        )
        await self.task_manager.operations_runtime.run(
            self.task_manager.append_history_entry,
            history_task_id,
            (
                f"Stopped local execution after exceeding the tool-call limit "
                f"({limit_error.observed_tool_calls} > {limit_error.max_tool_calls}). "
                f"Last attempted tool: {limit_error.last_attempted_tool or 'unknown'}."
            ),
            entry_type=TaskHistoryType.ERROR_EVENT,
            importance=TaskHistoryImportance.CRITICAL,
            details={
                "reason": "tool_call_limit_exceeded",
                "max_tool_calls_per_task": limit_error.max_tool_calls,
                "observed_tool_calls": limit_error.observed_tool_calls,
                "last_attempted_tool": limit_error.last_attempted_tool,
                "tool_counts_summary": count_summary,
                "tool_sequence": limit_error.tool_sequence[:25],
                "recent_attempted_tools": limit_error.recent_tool_uses[-8:],
                "recent_strands_tool_transcript": recent_tool_transcript,
                "execution_digest_markdown": execution_digest,
                "recent_history_markdown": recent_history,
            },
        )

    async def _resolve_control_target_task_info(
        self,
        *,
        control_instruction: ControlInstruction,
        msg_meta: MessageMeta,
    ) -> Optional[TaskInfo]:
        return await resolve_control_target_task_info(
            self,
            control_instruction=control_instruction,
            msg_meta=msg_meta,
        )

    def _set_active_execution_context(self, task_id: Optional[str]) -> ToolStop:
        """Mark the current task as the running turn; returns the turn's new ToolStop."""

        self._active_execution_task = asyncio.current_task()
        self._active_execution_task_id = task_id
        self._active_tool_stop = ToolStop(agent_id=self.id, task_id=task_id)
        return self._active_tool_stop

    def _clear_active_execution_context(self) -> None:
        current_task = asyncio.current_task()
        if self._active_execution_task is current_task or self._active_execution_task is None:
            # The turn's person model goes with it (custom_agents.person_model_runtime).
            clear_turn_model(self)
        if self._active_execution_task is current_task:
            self._active_execution_task = None
            self._active_execution_task_id = None
            self._active_tool_stop = None

    def _consume_stop_reason(self, task_id: str) -> Optional[str]:
        return self._stop_reasons_by_task_id.pop(task_id, None)

    def _raise_if_stop_requested(self, task_id: Optional[str]) -> None:
        if task_id and task_id in self._stop_reasons_by_task_id:
            raise asyncio.CancelledError

    def _validate_task_turn_llm_output(
        self,
        *,
        incoming_content: ContentType,
        outgoing_content: ContentType,
    ) -> None:
        if isinstance(incoming_content, Task):
            if isinstance(outgoing_content, Task):
                if (
                    outgoing_content.task_type == MessageMetaType.RESPONSE
                    and not outgoing_content.errors
                ):
                    quality_error = _terminal_answer_quality_error(
                        outgoing_content.answer
                    )
                    if quality_error:
                        answer_preview = _compact_debug_preview(
                            _task_answer_text(outgoing_content.answer),
                            max_chars=240,
                        )
                        self._logger.warning(
                            "[%s] Refusing malformed terminal Task response for task %s: %s answer_preview=%s",
                            self.id,
                            incoming_content.task_id,
                            quality_error,
                            answer_preview,
                        )
                        raise RuntimeError(
                            "Terminal Task response answer failed semantic validation: "
                            f"{quality_error}. Return a complete user-visible answer in task.answer."
                        )
                return
            if (
                isinstance(outgoing_content, TaskQuery)
                and outgoing_content.query_type == MessageMetaType.REQUEST
            ):
                return
            if (
                isinstance(outgoing_content, SecureCredentialCollectionRequest)
                and outgoing_content.request_type == MessageMetaType.REQUEST
            ):
                return
            if (
                isinstance(outgoing_content, SignupAutomationRequest)
                and outgoing_content.request_type == MessageMetaType.REQUEST
            ):
                return
            raise RuntimeError(
                f"Unsupported final LLM message type {type(outgoing_content).__name__} while handling "
                f"Task {incoming_content.task_id}. Task turns must end with a Task, TaskQuery request, "
                "or secure credential, tool setup, or signup request."
            )

        if isinstance(incoming_content, TaskQuery):
            if (
                isinstance(outgoing_content, TaskQuery)
                and outgoing_content.query_type == MessageMetaType.RESPONSE
            ):
                return
            raise RuntimeError(
                f"Unsupported final LLM message type {type(outgoing_content).__name__} while handling "
                f"TaskQuery {incoming_content.task_query_id}. TaskQuery turns must end with a TaskQuery response."
            )

    def _validate_outbound_request_routing(
        self,
        *,
        content: ContentType,
        header: MessageHeader,
        meta: Optional[MessageMeta],
        incoming_content: Optional[ContentType] = None,
        structured_output_details: Optional[Dict[str, Any]] = None,
        parsed_comms_snapshot: Optional[Dict[str, Any]] = None,
    ) -> None:
        is_task_request = (
            isinstance(content, Task) and content.task_type == MessageMetaType.REQUEST
        )
        if not is_task_request:
            return
        if header.to_routing_path != RoutingType.NULL_ROUTE.value:
            return

        task_id = content.task_id or "unknown"
        parent_task_id = (
            content.parent_task_id
            or getattr(incoming_content, "task_id", None)
            or "unknown"
        )
        target_named_agent_type = (
            getattr(meta, "to_named_agent_type", None) if meta else None
        )
        target_agent_name = getattr(meta, "to_agent_name", None) if meta else None
        debug_parts = []
        if parsed_comms_snapshot:
            debug_parts.append(
                "Parsed CommsMessage snapshot: "
                + _compact_debug_preview(parsed_comms_snapshot, max_chars=1600)
            )
        if structured_output_details:
            compact_structured_details = dict(structured_output_details)
            compact_structured_details.pop("input_preview", None)
            debug_parts.append(
                "Last structured-output tool input details: "
                + _compact_debug_preview(compact_structured_details, max_chars=1800)
            )
        debug_suffix = (" " + " ".join(debug_parts)) if debug_parts else ""
        raise RuntimeError(
            f"Refusing to dispatch unroutable Task request {task_id} for parent task {parent_task_id}: "
            f"resolved routing path is {RoutingType.NULL_ROUTE.value}. "
            f"LLM routing intent target_named_agent_type={target_named_agent_type or 'missing'} "
            f"target_agent_name={target_agent_name or 'missing'}. "
            "For delegated work, the structured response must set target_named_agent_type or target_agent_name; "
            "for a direct answer, return a Task response instead of a new Task request."
            f"{debug_suffix}"
        )

    async def _send_control_status_response(
        self,
        *,
        msg_header: MessageHeader,
        msg_meta: MessageMeta,
        status_text: str,
    ) -> None:
        await send_control_status_response(
            self,
            msg_header=msg_header,
            msg_meta=msg_meta,
            status_text=status_text,
        )

    async def _send_task_cancel_response(
        self,
        task_request: Task,
        msg_header: MessageHeader,
        msg_meta: MessageMeta,
        cancel_reason: str,
    ) -> None:
        await send_task_cancel_response(
            self,
            task_request,
            msg_header,
            msg_meta,
            cancel_reason,
        )

    async def _request_subtask_stop(
        self,
        *,
        parent_task_id: str,
        subtask_info: TaskInfo,
        reason: str,
    ) -> bool:
        return await request_subtask_stop(
            self,
            parent_task_id=parent_task_id,
            subtask_info=subtask_info,
            reason=reason,
        )

    async def _cancel_task_tree_locally(
        self,
        *,
        task_info: TaskInfo,
        reason: str,
        signal_pending_subtasks: bool,
    ) -> Dict[str, Any]:
        return await cancel_task_tree_locally(
            self,
            task_info=task_info,
            reason=reason,
            signal_pending_subtasks=signal_pending_subtasks,
        )

    async def _stop_task_execution(
        self,
        *,
        task_info: TaskInfo,
        reason: str,
        cascade_to_subtasks: bool,
    ) -> Dict[str, Any]:
        return await stop_task_execution(
            self,
            task_info=task_info,
            reason=reason,
            cascade_to_subtasks=cascade_to_subtasks,
        )

    def _should_fail_stale_executing_recovery(
        self, task_info: TaskInfo, now_ts: str
    ) -> tuple[bool, Optional[float]]:
        return should_fail_stale_executing_recovery(self, task_info, now_ts)

    async def _fail_recovered_executing_task(
        self,
        task_info: TaskInfo,
        *,
        now_ts: str,
        reason: str,
        snapshot_age_seconds: Optional[float],
    ) -> None:
        await fail_recovered_executing_task(
            self,
            task_info,
            now_ts=now_ts,
            reason=reason,
            snapshot_age_seconds=snapshot_age_seconds,
        )

    def _build_status_metrics(self) -> Dict[str, Any]:
        return {
            "execution_state": "executing" if self.is_executing else "idle",
            "has_execution_capacity": self._has_execution_capacity(),
            "tracked_top_level_task_count": self.task_manager.get_tracked_top_level_task_count(),
            "top_level_queue_depth": self.task_manager.get_runnable_top_level_queue_depth(),
            "waiting_top_level_task_count": self.task_manager.get_waiting_top_level_task_count(),
            "max_queued_top_level_tasks": self.task_manager.get_max_queued_top_level_tasks(),
            "deferred_message_count": len(self.deferred_inbox),
        }

    @property
    def instance_timezone(self) -> Optional[str]:
        """The instance time zone (``user.timezone``, or ``DEFAULT_REQUEST_TIMEZONE``).

        Cron schedules without an owner's time zone run in it (``schedule_store.fill_cron_timezone``).
        """

        return effective_default_request_timezone(self.default_request_timezone, logger=self._logger, agent_id=self.id)

    def _ensure_request_timezone(
        self, msg_meta: MessageMeta, *, top_level_request: bool
    ) -> MessageMeta:
        return ensure_request_timezone(
            msg_meta,
            top_level_request=top_level_request,
            default_request_timezone=self.default_request_timezone,
            logger=self._logger,
            agent_id=self.id,
        )

    _queue_pressure_notifications: QueuePressureNotifications = PrivateAttr(default_factory=QueuePressureNotifications)

    _dead_letter_watch: DeadLetterWatchState = PrivateAttr(default_factory=DeadLetterWatchState)

    _agent_state_io: BlockingIOLane = PrivateAttr(default_factory=BlockingIOLane)

    async def _refresh_self_agent_state(self, *, is_exiting: bool = False) -> None:
        if self._agent_state_store is None:
            return
        try:
            effective_is_exiting = is_exiting or self._should_exit.is_set()
            await self._agent_state_io.run(
                self._agent_state_store.upsert,
                self.get_self_status(is_exiting=effective_is_exiting),
            )
        except Exception as exc:
            self._logger.warning(
                f"[{self.id}] Failed to refresh self AgentState row: {exc}"
            )

    async def _resume_parent_task_if_ready(self, *args, **kwargs):
        async with self._execution_scope():
            return await subtask_watchdog_runtime._resume_parent_task_if_ready(
                self, *args, **kwargs
            )

    async def _fail_subtask_and_reconcile_parent(self, *args, **kwargs):
        return await subtask_watchdog_runtime._fail_subtask_and_reconcile_parent(
            self, *args, **kwargs
        )

    async def _prepare_local_task_finalization_before_dispatch(self, *args, **kwargs):
        return await subtask_watchdog_runtime._prepare_local_task_finalization_before_dispatch(
            self, *args, **kwargs
        )

    async def _complete_local_task_finalization_after_dispatch(self, *args, **kwargs):
        return await subtask_watchdog_runtime._complete_local_task_finalization_after_dispatch(
            self, *args, **kwargs
        )

    async def _probe_subtask_task_status(self, *args, **kwargs):
        return await subtask_watchdog_runtime._probe_subtask_task_status(
            self, *args, **kwargs
        )

    def _task_status_extends_subtask_wait(self, *args, **kwargs):
        return subtask_watchdog_runtime._task_status_extends_subtask_wait(
            self, *args, **kwargs
        )

    def _format_subtask_timeout_threshold(self, *args, **kwargs):
        return subtask_watchdog_runtime._format_subtask_timeout_threshold(
            *args, **kwargs
        )

    async def _record_subtask_timeout_probe(self, *args, **kwargs):
        return await subtask_watchdog_runtime._record_subtask_timeout_probe(
            self, *args, **kwargs
        )

    async def _attempted_subtask_timeout_thresholds(self, *args, **kwargs):
        return await subtask_watchdog_runtime._attempted_subtask_timeout_thresholds(
            self, *args, **kwargs
        )

    async def _next_due_subtask_timeout_threshold(self, *args, **kwargs):
        return await subtask_watchdog_runtime._next_due_subtask_timeout_threshold(
            self, *args, **kwargs
        )

    async def _subtask_timeout_probe_summary(self, *args, **kwargs):
        return await subtask_watchdog_runtime._subtask_timeout_probe_summary(
            self, *args, **kwargs
        )

    async def _recover_stalled_subtasks(self, *args, **kwargs):
        return await subtask_watchdog_runtime._recover_stalled_subtasks(
            self, *args, **kwargs
        )

    async def _discard_runtime_state_for_team_change(
        self, previous_team_id: str
    ) -> None:
        await discard_runtime_state_for_team_change(self, previous_team_id)

    async def _recover_runtime_after_restart(self) -> None:
        await recover_runtime_after_restart(self)
        stale_lease_count = await self.task_manager.operations_runtime.run(
            self._runtime_coordinator().recover_after_restart
        )
        if stale_lease_count:
            self._logger.warning(
                "[%s] event=runtime_coordinator_recovered_stale_leases count=%s",
                self.id,
                stale_lease_count,
            )

    async def _send_task_error_response(
        self,
        task_request: Task,
        msg_header: MessageHeader,
        msg_meta: MessageMeta,
        error_message: str,
    ) -> None:
        failed_at = datetime.now(timezone.utc).isoformat()
        error_list = task_message_runtime._task_error_response_errors(
            task_request.errors,
            error_message,
        )
        task_state = list(task_request.task_state or [])
        if not task_state or task_state[-1][0] != ProgressState.FAILED:
            task_state.append((ProgressState.FAILED, failed_at))
        error_task = Task(
            task_id=task_request.task_id,
            parent_task_id=task_request.parent_task_id,
            task_state=task_state,
            task_type=MessageMetaType.RESPONSE,
            problem_statement=task_request.problem_statement,
            errors=error_list,
            task_plan=task_request.task_plan,
            execution_history=list(task_request.execution_history or []),
            answer=error_message,
            failure=task_request.failure,
        )
        error_meta = await build_outgoing_meta(
            llm_meta=MessageMeta(conversation_id=msg_meta.conversation_id,
                                 gateway_context=msg_meta.gateway_context),
            incoming_meta=msg_meta,
            content=error_task,
            from_agent_name=self.name,
            repair_recorder=self._record_runtime_repair_anomaly,
        )
        header = await construct_header(
            comms_message=CommsMessage(msg_content=error_task, msg_meta=error_meta),
            explicit_to_path=None,
            route_response_to_self=False,
            agent_id=self.id,
            agent_name=self.name,
            team_id=self.team_id,
            named_agent_type_queue_map=self.named_agent_type_queue_map,
            agent_id_by_name=self._agent_id_by_name,
            logger=self._logger,
            repair_recorder=self._record_runtime_repair_anomaly,
        )
        routing_key = await resolve_outgoing_routing(
            content=error_task,
            header=header,
            incoming_header=msg_header,
            incoming_meta=msg_meta,
            agent_id_by_name=self._agent_id_by_name,
            named_agent_type_queue_map=self.named_agent_type_queue_map,
            team_id=self.team_id,
            logger=self._logger,
            agent_id=self.id,
            repair_recorder=self._record_runtime_repair_anomaly,
        )
        publish_team_id = self._extract_team_id_from_routing_value(routing_key)
        await self.team_comms.send_message(
            message=MessageEnvelope(
                msg_content=error_task, msg_meta=error_meta, msg_header=header
            ),
            routing_key=routing_key,
            team_id=publish_team_id
            if publish_team_id and publish_team_id != self.team_id
            else None,
        )

    def _ensure_schedule_store(self) -> ScheduleStore:
        if self._schedule_store is None:
            self._schedule_store = ScheduleStore(
                db_path=self.task_manager._db_path,
                logger=self._logger,
                storage_settings=self.task_manager.storage_settings,
                default_cron_timezone=self.instance_timezone,
            )
        return self._schedule_store

    async def _schedule_provider_cooldown_retry(
        self,
        *,
        task_info: TaskInfo,
        retry_at: datetime,
        marker: str = PROVIDER_COOLDOWN_RETRY_MARKER,
        deferred_handoff: Optional[DeferredHandoff] = None,
    ) -> ScheduledMessage:
        """Store a one-time retry of a task paused by a model provider's limits.

        A delegated task waiting out a provider limit (custom_agents.provider_limits)
        uses the cooldown marker; a person's retry choice
        (``_schedule_provider_overload_retry``) uses its pause or query id.
        """

        schedule = ScheduledMessage(
            schedule_id=new_id("prov_ovld_retry"),
            schedule_name=f"{PROVIDER_OVERLOAD_RETRY_SCHEDULE_NAME_PREFIX}{task_info.task.task_id}:{marker}",
            management_class=ScheduleManagementClass.SYSTEM,
            system_schedule_kind=SystemScheduleKind.PROVIDER_OVERLOAD_RETRY,
            payload=task_info.task.model_copy(deep=True),
            payload_header=task_info.header.model_copy(deep=True),
            payload_meta=task_info.meta.model_copy(deep=True),
            repeat_count=1,
            next_fire_at=retry_at.isoformat(),
        )
        def install_retry() -> None:
            # This insertion completes a delayed-retry decision. Consume its
            # reply in the same transaction, before a shutdown can replay it.
            if deferred_handoff is not None and isinstance(deferred_handoff.failure, RuntimeCustodyLost):
                raise deferred_handoff.failure
            try:
                self._ensure_schedule_store().add(
                    schedule,
                    deferred_message=None if deferred_handoff is None else DeferredMessageClaim(
                        self.id, deferred_handoff.message_id,
                    ),
                    **schedule_claim_scope_for_agent(self),
                )
            except RuntimeCustodyLost as lost:
                if deferred_handoff is not None:
                    deferred_handoff.failure = lost
                raise
            if deferred_handoff is not None:
                # The lane waits through cancellation until this operation
                # finishes, so the inbox also observes a committed handoff.
                deferred_handoff.committed = True

        await self.task_manager.operations_runtime.run(install_retry)
        if self._scheduler_wake is not None:
            self._scheduler_wake.set()
        return schedule

    async def _schedule_provider_overload_retry(
        self,
        *,
        task_info: TaskInfo,
        delay_minutes: int,
        decision: str,
        pause_id: Optional[str],
        task_query_id: Optional[str],
        timestamp: str,
    ) -> ScheduledMessage:
        retry_at = datetime.now(timezone.utc) + timedelta(minutes=delay_minutes)
        schedule = await self._schedule_provider_cooldown_retry(
            task_info=task_info,
            retry_at=retry_at,
            marker=pause_id or task_query_id or "unknown",
            deferred_handoff=pending_deferred_handoff(),
        )
        await self.task_manager.operations_runtime.run(
            self.task_manager.append_history_entry,
            task_info.task.task_id,
            f"Provider-overload retry scheduled for {retry_at.isoformat()}.",
            source=TaskHistorySource.SYSTEM,
            entry_type=TaskHistoryType.QUERY_EVENT,
            importance=TaskHistoryImportance.HIGH,
            timestamp=timestamp,
            related_task_id=task_query_id,
            details={
                "reason": "provider_overload_retry_scheduled",
                "decision": decision,
                "delay_minutes": delay_minutes,
                "retry_at": retry_at.isoformat(),
                "schedule_id": schedule.schedule_id,
                "pause_id": pause_id,
            },
        )
        self._logger.info(
            "[%s] Scheduled provider-overload retry for task %s in %s minutes via %s.",
            self.id,
            task_info.task.task_id,
            delay_minutes,
            schedule.schedule_id,
        )
        return schedule

    async def _send_runtime_pause_query(
        self,
        *,
        task_info: TaskInfo,
        trigger: RecoverablePauseTrigger,
        friendly_reason: str,
        base_tool_call_limit: Optional[int] = None,
        affected_subtask_id: Optional[str] = None,
        affected_agent_id: Optional[str] = None,
        affected_named_agent_type: Optional[str] = None,
    ) -> TaskQuery:
        now_ts = datetime.now(timezone.utc).isoformat()
        allowed_decisions = self._runtime_pause_allowed_decisions(trigger)
        query = TaskQuery(
            query_type=MessageMetaType.REQUEST,
            parent_task_id=task_info.task.task_id,
            message=(
                runtime_pause_identity(
                    task_id=task_info.task.task_id, description=task_info.task.problem_statement,
                    subtask_id=affected_subtask_id, worker=affected_named_agent_type or affected_agent_id,
                )
                + self._runtime_pause_prompt(friendly_reason, trigger=trigger)
            ),
            query_reason=TaskQueryReason.RECOVERABLE_RUNTIME_PAUSE,
            allowed_decisions=allowed_decisions,
            recoverable_pause_trigger=trigger,
        )
        record = await self.task_manager.operations_runtime.run(
            self.task_manager.create_recoverable_pause,
            task_info.task.task_id,
            trigger=trigger,
            friendly_reason=friendly_reason,
            task_query_id=query.task_query_id,
            affected_subtask_id=affected_subtask_id,
            affected_agent_id=affected_agent_id,
            affected_named_agent_type=affected_named_agent_type,
            base_tool_call_limit=base_tool_call_limit,
            partial_tool_call_limit=self._partial_tool_call_limit(base_tool_call_limit),
            timestamp=now_ts,
        )
        await self.task_manager.operations_runtime.run(
            self.task_manager.add_task_query,
            task_info.task.task_id,
            query,
            agent_name=self.name,
            agent_id=self.id,
            timestamp=now_ts,
        )
        await self.task_manager.operations_runtime.run(
            self.task_manager.update_main_task_state,
            task_info.task.task_id,
            ProgressState.PAUSED,
            now_ts,
        )
        plan_id = await self.task_manager.operations_runtime.run(
            self.task_manager.get_task_plan_id, task_info.task.task_id
        )
        if plan_id:
            await self.task_manager.operations_runtime.run(
                self.task_manager.record_plan_action,
                task_info.task.task_id,
                PlanActionType.PAUSE_TASK,
                f"Paused task for requester guidance: {friendly_reason}",
                status=PlanActionStatus.PENDING,
                reason=trigger.value,
                related_task_id=affected_subtask_id,
                user_visible=True,
                details={
                    "pause_id": record.pause_id,
                    "task_query_id": query.task_query_id,
                },
                agent_name=self.name,
                agent_id=self.id,
                timestamp=now_ts,
            )

        outgoing_meta = await build_outgoing_meta(
            llm_meta=MessageMeta(conversation_id=task_info.meta.conversation_id),
            incoming_meta=task_info.meta,
            content=query,
            from_agent_name=self.name,
            repair_recorder=self._record_runtime_repair_anomaly,
        )
        header = await construct_header(
            comms_message=CommsMessage(msg_content=query, msg_meta=outgoing_meta),
            explicit_to_path=None,
            route_response_to_self=False,
            agent_id=self.id,
            agent_name=self.name,
            team_id=self.team_id,
            named_agent_type_queue_map=self.named_agent_type_queue_map,
            agent_id_by_name=self._agent_id_by_name,
            logger=self._logger,
            repair_recorder=self._record_runtime_repair_anomaly,
        )
        routing_key = await resolve_outgoing_routing(
            content=query,
            header=header,
            incoming_header=task_info.header,
            incoming_meta=task_info.meta,
            agent_id_by_name=self._agent_id_by_name,
            named_agent_type_queue_map=self.named_agent_type_queue_map,
            team_id=self.team_id,
            logger=self._logger,
            agent_id=self.id,
            repair_recorder=self._record_runtime_repair_anomaly,
        )
        await self.team_comms.send_message(
            message=MessageEnvelope(
                msg_content=query, msg_meta=outgoing_meta, msg_header=header
            ),
            routing_key=routing_key,
        )
        return query

    async def _send_paused_subtask_response(
        self,
        *,
        task_request: Task,
        msg_header: MessageHeader,
        msg_meta: MessageMeta,
        error_message: str,
        trigger: RecoverablePauseTrigger,
    ) -> Task:
        paused_at = datetime.now(timezone.utc).isoformat()
        error_list = list(task_request.errors or [])
        if error_message not in error_list:
            error_list.append(error_message)
        task_state = list(task_request.task_state or [])
        task_state.append((ProgressState.PAUSED, paused_at))
        paused_task = Task(
            task_id=task_request.task_id,
            parent_task_id=task_request.parent_task_id,
            task_state=task_state,
            task_type=MessageMetaType.RESPONSE,
            problem_statement=task_request.problem_statement,
            errors=error_list,
            task_plan=task_request.task_plan,
            execution_history=list(task_request.execution_history or []),
            answer=f"Delegated work paused: {error_message}",
        )
        paused_task.execution_history.append(
            TaskHistoryEntry(
                summary=error_message,
                entry_type=TaskHistoryType.ERROR_EVENT,
                importance=TaskHistoryImportance.CRITICAL,
                timestamp=paused_at,
                agent_name=self.name,
                agent_id=self.id,
                details={
                    "reason": "recoverable_runtime_pause",
                    "trigger": trigger.value,
                    "named_agent_type": self.named_agent_type,
                },
            )
        )
        outgoing_meta = await build_outgoing_meta(
            llm_meta=MessageMeta(conversation_id=msg_meta.conversation_id),
            incoming_meta=msg_meta,
            content=paused_task,
            from_agent_name=self.name,
            repair_recorder=self._record_runtime_repair_anomaly,
        )
        header = await construct_header(
            comms_message=CommsMessage(msg_content=paused_task, msg_meta=outgoing_meta),
            explicit_to_path=None,
            route_response_to_self=False,
            agent_id=self.id,
            agent_name=self.name,
            team_id=self.team_id,
            named_agent_type_queue_map=self.named_agent_type_queue_map,
            agent_id_by_name=self._agent_id_by_name,
            logger=self._logger,
            repair_recorder=self._record_runtime_repair_anomaly,
        )
        routing_key = await resolve_outgoing_routing(
            content=paused_task,
            header=header,
            incoming_header=msg_header,
            incoming_meta=msg_meta,
            agent_id_by_name=self._agent_id_by_name,
            named_agent_type_queue_map=self.named_agent_type_queue_map,
            team_id=self.team_id,
            logger=self._logger,
            agent_id=self.id,
            repair_recorder=self._record_runtime_repair_anomaly,
        )
        await self.team_comms.send_message(
            message=MessageEnvelope(
                msg_content=paused_task, msg_meta=outgoing_meta, msg_header=header
            ),
            routing_key=routing_key,
        )
        return paused_task

    async def _send_after_action_report_followup(
        self,
        *,
        report: AfterActionReport,
        source_task: Task,
        incoming_header: MessageHeader,
        incoming_meta: MessageMeta,
    ) -> bool:
        """Send a non-blocking AfterActionReport follow-up through the reply path."""
        if not source_task.parent_task_id:
            self._logger.warning(
                "[%s] Skipping AfterActionReport %s for top-level task %s; "
                "AAR follow-ups are currently specialist-to-manager messages only.",
                self.id,
                report.report_id,
                source_task.task_id,
            )
            return False
        # The report is about this task: its links come from the task, never
        # from the model, because the delegating agent records it for the
        # person that task's parent works for (after_action_report_identity).
        if (report.source_task_id or source_task.task_id) != source_task.task_id or (
            report.parent_task_id or source_task.parent_task_id
        ) != source_task.parent_task_id:
            self._logger.warning(
                "[%s] AfterActionReport %s named other task links than its own task; using the task's. "
                "event=after_action_report_links_replaced named_source_task_id=%s named_parent_task_id=%s "
                "source_task_id=%s parent_task_id=%s",
                self.id,
                report.report_id,
                report.source_task_id,
                report.parent_task_id,
                source_task.task_id,
                source_task.parent_task_id,
            )
        report.source_task_id = source_task.task_id
        report.parent_task_id = source_task.parent_task_id
        if not report.source_agent_id:
            report.source_agent_id = self.id
        if not report.source_agent_name:
            report.source_agent_name = self.name
        if not report.source_named_agent_type:
            report.source_named_agent_type = self.named_agent_type

        report_comms = CommsMessage(msg_content=report, msg_meta=MessageMeta())
        header = await construct_header(
            comms_message=report_comms,
            explicit_to_path=None,
            route_response_to_self=False,
            agent_id=self.id,
            agent_name=self.name,
            team_id=self.team_id,
            named_agent_type_queue_map=self.named_agent_type_queue_map,
            agent_id_by_name=self._agent_id_by_name,
            logger=self._logger,
            repair_recorder=self._record_runtime_repair_anomaly,
        )
        meta = await build_outgoing_meta(
            llm_meta=report_comms.msg_meta,
            incoming_meta=incoming_meta,
            content=report,
            from_agent_name=self.name,
            repair_recorder=self._record_runtime_repair_anomaly,
        )
        routing_key = await resolve_outgoing_routing(
            content=report,
            header=header,
            incoming_header=incoming_header,
            incoming_meta=incoming_meta,
            agent_id_by_name=self._agent_id_by_name,
            named_agent_type_queue_map=self.named_agent_type_queue_map,
            team_id=self.team_id,
            logger=self._logger,
            agent_id=self.id,
            repair_recorder=self._record_runtime_repair_anomaly,
        )
        if not routing_key or routing_key == RoutingType.NULL_ROUTE.value:
            self._logger.warning(
                "[%s] Could not route AfterActionReport %s for source_task_id=%s parent_task_id=%s.",
                self.id,
                report.report_id,
                report.source_task_id,
                report.parent_task_id,
            )
            return False

        await self.team_comms.send_message(
            message=MessageEnvelope(
                msg_content=report, msg_meta=meta, msg_header=header
            ),
            routing_key=routing_key,
        )
        self._logger.info(
            "[%s] Sent AfterActionReport %s for source_task_id=%s parent_task_id=%s to %s.",
            self.id,
            report.report_id,
            report.source_task_id,
            report.parent_task_id,
            routing_key,
        )
        return True

    async def _handle_after_action_report(
        self,
        report: AfterActionReport,
        msg_header: MessageHeader,
        msg_meta: MessageMeta,
    ) -> None:
        """Record AAR follow-up context without depending on active task state.

        The report is recorded for the person its work was for, from verified
        identity only (``custom_agents.after_action_report_identity``); a
        report that cannot be tied to them is not recorded (logged there).
        """
        from custom_agents.after_action_report_identity import reported_work_identity

        if not report.source_agent_id:
            report.source_agent_id = msg_header.from_agent_id
        if not report.source_agent_name:
            report.source_agent_name = getattr(msg_meta, "from_agent_name", None)
        target_task_id = report.parent_task_id or report.source_task_id
        parent = (
            await self.task_manager.operations_runtime.run(self.task_manager.get_task_info, target_task_id)
            if target_task_id
            else None
        )
        identity = await asyncio.to_thread(
            reported_work_identity,
            self,
            report=report,
            report_meta=msg_meta,
            parent_meta=parent.meta if parent is not None else None,
        )
        if identity is None:
            return
        recorded = await self.task_manager.operations_runtime.run(
            self.task_manager.record_after_action_report,
            report,
            identity=identity,
            agent_name=report.source_agent_name
            or getattr(msg_meta, "from_agent_name", None),
            agent_id=report.source_agent_id or msg_header.from_agent_id,
            timestamp=getattr(msg_header, "timestamp", None),
        )
        if recorded is None:
            self._logger.warning(
                "[%s] AfterActionReport %s could not be associated with active or archived task state "
                "(source_task_id=%s parent_task_id=%s).",
                self.id,
                report.report_id,
                report.source_task_id,
                report.parent_task_id,
            )
            return
        if (
            recorded.accepted
            and recorded.batch is not None
            and (recorded.schedule_required or recorded.batch.scheduled_message_id)
        ):
            await self._schedule_after_action_report_batch_review(recorded.batch)
        self._logger.info(
            "[%s] Recorded AfterActionReport %s for manager review on task %s (accepted=%s stale=%s batch_id=%s).",
            self.id,
            report.report_id,
            report.parent_task_id or report.source_task_id,
            recorded.accepted,
            recorded.stale,
            recorded.batch.batch_id if recorded.batch else None,
        )

    async def _schedule_after_action_report_batch_review(self, batch) -> Optional[str]:
        """Schedule the batch's review, owned by the person the reports are about.

        The review schedule is runtime-managed and owned by the batch's person
        (none for work for nobody), so the scheduler signs each review run for
        them (``custom_agents.after_action_report_identity``).
        """
        from custom_agents.after_action_report_identity import review_gateway_context

        gateway_context = review_gateway_context(batch)
        if gateway_context is None:
            self._logger.error(
                "[%s] Not scheduling a review of AAR batch %s: it was saved without a verified person. "
                "event=after_action_review_not_scheduled_unverified",
                self.id,
                batch.batch_id,
            )
            return None
        schedule_id = batch.scheduled_message_id or new_id("sched_aar")
        review_task_id = batch.scheduled_review_task_id or new_id("task_aar_review")
        if self._schedule_store is None:
            self._schedule_store = ScheduleStore(
                db_path=self.task_manager._db_path,
                logger=self._logger,
                storage_settings=self.task_manager.storage_settings,
                default_cron_timezone=self.instance_timezone,
            )
        review_task = await self.task_manager.operations_runtime.run(
            self.task_manager.build_after_action_report_batch_review_task,
            batch.batch_id,
            task_id=review_task_id,
        )
        if review_task is None:
            self._logger.warning(
                "[%s] Could not build scheduled AAR review task for batch %s.",
                self.id,
                batch.batch_id,
            )
            return None
        scheduled = ScheduledMessage(
            schedule_id=schedule_id,
            schedule_name=f"{AFTER_ACTION_REPORT_REVIEW_SCHEDULE_NAME_PREFIX}{batch.batch_id}",
            management_class=ScheduleManagementClass.SYSTEM,
            system_schedule_kind=SystemScheduleKind.AFTER_ACTION_REPORT_REVIEW,
            payload=review_task,
            payload_header=MessageHeader(
                from_agent_id=self.id,
                to_routing_path=RoutingType.SELF_ROUTE.value,
                agent_signature="",
            ),
            payload_meta=MessageMeta(
                from_agent_name=self.name,
                requester_type=RequesterType.TIMER_REQUESTER,
                is_response_requested=False,
                gateway_context=gateway_context,
            ),
            repeat_count=1,
            next_fire_at=batch.scheduled_review_at,
        )
        await self.task_manager.operations_runtime.run(
            self._schedule_store.add,
            scheduled,
            **schedule_claim_scope_for_agent(self),
        )
        await self.task_manager.operations_runtime.run(
            self.task_manager.mark_after_action_report_batch_scheduled,
            batch.batch_id,
            scheduled_message_id=schedule_id,
            scheduled_review_task_id=review_task.task_id,
        )
        if self._scheduler_wake is not None:
            self._scheduler_wake.set()
        self._logger.info(
            "[%s] Scheduled AAR batch review task %s via schedule %s for batch %s at %s.",
            self.id,
            review_task.task_id,
            schedule_id,
            batch.batch_id,
            batch.scheduled_review_at,
        )
        return schedule_id

    async def _complete_task_and_schedule_aar_review(
        self, task_id: str, timestamp: str
    ) -> None:
        batches = await self.task_manager.operations_runtime.run(
            self.task_manager.complete_task, task_id, timestamp
        )
        for batch in batches:
            await self._schedule_after_action_report_batch_review(batch)

    def _setup_team_lead_queues(self):
        """Sets up queue mappings for team lead agents."""
        for named_type in _local_named_agent_types(
            self._local_team_runtime,
            self.named_agent_type,
        ):
            self.named_agent_type_queue_map[named_type] = f"{self.team_id}_{named_type}"

        # Also include the user queue
        # user_queue_key = f'{self.team_id}_{self.user_name_postfix}'
        # self.named_agent_type_queue_map[user_queue_key] = user_queue_key

    def _create_strands_agent(self):
        """Creates the internal Strands Agent with configured model and tools."""
        try:
            model = self._strands_model or self._configure_model()
            self._strands_model = model
            self._refresh_skills_plugins(force=True)

            # Prepare the full system prompt with agent status placeholder
            full_system_prompt = self.system_prompt
            self._runtime_guardrails_plugin = RuntimeGuardrailsPlugin(
                structured_output_failure_threshold=self.structured_output_failure_threshold,
                max_tool_calls=self.max_tool_calls_per_task,
                logger_=self._logger,
            )
            self._runtime_steering_plugin = RuntimeSteeringPlugin(self)
            from custom_agents.access_runtime import AccessDecisionHook

            # Always installed: it decides every function tool call, and with
            # authorization off it allows every catalog tool (access_runtime).
            # It also withdraws access requests of tasks that end first.
            self._access_decision_hook = AccessDecisionHook(self)
            from custom_agents.tool_sync import install_tool_sync, startup_tools

            # With authorization on the model sees its role's standard tools at
            # first; the rest follow the grants valid for each task's person
            # (access Phase 7). In both modes installed MCP servers load before
            # model calls, not at startup (custom_agents.tool_sync).
            model_tools = startup_tools(self, list(self.tools))
            tool_sync = install_tool_sync(self, model_tools)
            from custom_agents.access_runtime import runtime_access
            from custom_agents.tool_loading import OnRequestToolHook, ToolLimitHook, loadable_tools_text

            # The groups that load on request, listed by name and purpose, cost
            # far less than their definitions in every call (custom_agents.tool_loading).
            loadable_text = loadable_tools_text(self, runtime_access(self).catalog)
            if loadable_text:
                full_system_prompt = f"{full_system_prompt}\n\n{loadable_text}"
            tool_result_offloader = TaskAwareToolResultOffloader(
                max_tokens_per_call=self.max_tool_result_tokens_per_call,
                model=model,
                artifact_root=agent_layout(self).tool_results_dir,
                task_manager=self.task_manager,
                team_id=self.team_id,
                agent_id=self.id,
                named_agent_type=self.named_agent_type,
                logger_=self._logger,
            )
            self._tool_result_budget_monitor = tool_result_offloader
            conversation_manager = (
                self._context_engine.build_inner_loop_conversation_manager(
                    tool_result_offloader.storage
                )
                if self._context_engine is not None
                else None
            )
            agent_signature = inspect.signature(Agent)
            retry_strategy_config = self._model_failover_retry_strategy_config()
            agent_kwargs: Dict[str, Any] = {}
            if (
                retry_strategy_config is not None
                and "retry_strategy" in agent_signature.parameters
            ):
                from strands.event_loop._retry import ModelRetryStrategy

                agent_kwargs["retry_strategy"] = ModelRetryStrategy(
                    **retry_strategy_config
                )

            agent_hooks = [
                self._tool_result_budget_monitor,
                self._runtime_guardrails_plugin,
                self._runtime_steering_plugin,
                self._access_decision_hook,
                tool_sync,
                PersonModelHook(self),
                ToolLimitHook(self),
                OnRequestToolHook(self),
            ]
            if "plugins" in agent_signature.parameters:
                agent_kwargs["plugins"] = list(self.plugins or [])
            elif self.plugins:
                agent_hooks.extend(self.plugins)

            # Create Strands Agent
            self._strands_agent = Agent(
                system_prompt=full_system_prompt,
                tools=model_tools,
                model=model,
                conversation_manager=conversation_manager,
                callback_handler=None,
                hooks=agent_hooks,
                **agent_kwargs,
            )
            if self._context_debug_dump_plugin is not None:
                register_context_debug_model_observer(
                    self._strands_agent,
                    self._context_debug_dump_plugin,
                    logger=self._logger,
                )

            self._logger.info(
                f"[{self.id}] Strands Agent created with {len(self.tools)} tools and {len(self.plugins)} plugins"
            )

        except Exception as e:
            self._logger.error(f"[{self.id}] Failed to create Strands Agent: {e}")
            raise

    def apply_runtime_llm_override(
        self,
        provider: str,
        model_id: str,
        *,
        reasoning_effort: Any = _UNSET,
        reasoning_summary: Any = _UNSET,
        litellm_api_mode: Any = _UNSET,
        context_window_tokens: Any = _UNSET,
        supports_image_input: Any = _UNSET,
        supports_audio_input: Any = _UNSET,
        supports_file_input: Any = _UNSET,
        source: str = "persistent_override",
    ) -> None:
        overrides: dict[str, Any] = {
            "provider": provider,
            "model_id": model_id,
        }
        optional_values = {
            "reasoning_effort": reasoning_effort,
            "reasoning_summary": reasoning_summary,
            "litellm_api_mode": litellm_api_mode,
            "context_window_tokens": context_window_tokens,
            "supports_image_input": supports_image_input,
            "supports_audio_input": supports_audio_input,
            "supports_file_input": supports_file_input,
        }
        overrides.update(
            {
                field: value
                for field, value in optional_values.items()
                if value is not _UNSET
            }
        )
        UniversalAgent.apply_runtime_llm_profile(
            self,
            runtime_llm_profile_from_agent(
                self,
                overrides=overrides,
                reset_model_specific_fields=True,
            ),
            source=source,
        )

    def probe_runtime_llm_profile(
        self,
        profile: RuntimeLLMProfile | dict[str, Any],
        *,
        request_settings: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Build and invoke a tool-free candidate before changing durable state."""
        candidate = RuntimeLLMProfile.model_validate(profile)
        model = self._configure_model_for_profile(candidate, request_settings=request_settings)
        timeout = self.request_timeout_seconds if request_settings is None else request_settings["request_timeout_seconds"]
        probe_result = probe_strands_model(
            model,
            timeout_seconds=float(
                timeout or LLM_DEFAULTS.request_timeout_seconds.value
            ),
        )
        return {
            **probe_result,
            "ready": True,
            "provider": candidate.provider,
            "model_id": candidate.model_id,
            "litellm_api_mode": candidate.litellm_api_mode,
        }

    def _rebuild_runtime_llm_components(self) -> None:
        """Rebuild every component whose behavior depends on the active model profile."""

        self._strands_model = self._configure_model()
        self._conversation_compactor = build_conversation_compactor(
            self.conversation_compactor_type,
            config=ConversationCompactionConfig(
                context_window_tokens=self._resolved_context_window_tokens(),
                trigger_ratio=self.context_compaction_trigger_ratio,
                target_ratio=self.context_compaction_target_ratio,
                min_recent_turns=self.context_compaction_min_recent_turns,
                max_recent_turns=self.context_compaction_max_recent_turns,
                trigger_turn_count=self.context_compaction_trigger_turn_count,
                trigger_message_count=self.context_compaction_trigger_message_count,
                compaction_model_provider=self.compaction_model_provider,
                compaction_model_id=self.compaction_model_id,
                default_model_provider=self.model_provider,
                default_model_id=self.model_id,
                temperature=self.temperature,
                litellm_api_base=self.litellm_api_base,
                litellm_api_mode=self.litellm_api_mode,
                openai_codex=self.openai_codex,
                xai_oauth=self.xai_oauth,
                instance_root=infer_instance_root(self),
            ),
            logger=self._logger,
        )
        self._conversation_summary_generator = build_conversation_summary_generator(
            self.conversation_summary_generator_type,
            config=self._conversation_summary_config(),
            logger=self._logger,
        )
        self._create_context_engine()
        self._create_strands_agent()

    def apply_runtime_llm_profile(
        self,
        profile: RuntimeLLMProfile | dict[str, Any],
        *,
        source: str = "persistent_override",
    ) -> None:
        """Apply one complete model profile and restore the old runtime on failure."""
        candidate = RuntimeLLMProfile.model_validate(profile)
        previous = runtime_llm_profile_from_agent(self)
        changed_fields = [
            field
            for field in (*MODEL_TUNING_FIELDS, *MODEL_CAPABILITY_FIELDS)
            if getattr(previous, field) != getattr(candidate, field)
        ]
        if previous.provider != candidate.provider:
            changed_fields.insert(0, "provider")
        if previous.model_id != candidate.model_id:
            changed_fields.insert(
                1 if changed_fields and changed_fields[0] == "provider" else 0,
                "model_id",
            )
        if not changed_fields:
            return

        rebuild_required = any(
            field in {"provider", "model_id", *MODEL_TUNING_FIELDS}
            for field in changed_fields
        )
        previous_references = {
            field: getattr(self, field) for field in RUNTIME_LLM_REFERENCES
        }

        self.model_provider = candidate.provider
        self.model_id = candidate.model_id
        for field in (*MODEL_TUNING_FIELDS, *MODEL_CAPABILITY_FIELDS):
            setattr(self, field, getattr(candidate, field))

        try:
            if rebuild_required:
                self._rebuild_runtime_llm_components()
        except Exception:
            self.model_provider = previous.provider
            self.model_id = previous.model_id
            for field in (*MODEL_TUNING_FIELDS, *MODEL_CAPABILITY_FIELDS):
                setattr(self, field, getattr(previous, field))
            for field, value in previous_references.items():
                setattr(self, field, value)
            self._logger.exception(
                "[%s] Failed to apply runtime LLM profile from %s; restored %s/%s.",
                self.id,
                source,
                previous.provider,
                previous.model_id,
            )
            raise

        log = (
            self._logger.info
            if source.startswith("persistent")
            else self._logger.warning
        )
        log(
            "[%s] Runtime LLM profile changed from %s: %s/%s -> %s/%s; fields=%s",
            self.id,
            source,
            previous.provider,
            previous.model_id,
            candidate.provider,
            candidate.model_id,
            ",".join(changed_fields),
        )

    def apply_runtime_context_dump_settings(
        self,
        *,
        enabled: bool,
        mode: str,
        source: str = "runtime_control",
    ) -> dict[str, Any]:
        normalized_mode = str(mode or "on_error").strip().lower()
        if normalized_mode not in {"always", "on_error", "on_budget_warning"}:
            raise ValueError(
                "Context dump mode must be one of: always, on_error, on_budget_warning."
            )

        debug_config = copy.deepcopy(self.debug or {})
        context_dump = merge_context_debug_dump_config(debug_config.get("context_dump"))
        context_dump["enabled"] = bool(enabled)
        context_dump["mode"] = normalized_mode
        debug_config["context_dump"] = context_dump
        self.debug = debug_config

        if self._context_debug_dump_plugin is not None:
            self._context_debug_dump_plugin.writer.config = (
                merge_context_debug_dump_config(context_dump)
            )

        self._logger.warning(
            "[%s] Runtime context debug dump settings changed from %s: enabled=%s mode=%s",
            self.id,
            source,
            bool(enabled),
            normalized_mode,
        )
        return {"enabled": bool(enabled), "mode": normalized_mode}

    def refresh_persistent_llm_override(self) -> None:
        resolved = load_persistent_llm_override(
            instance_root=self.instance_root,
            explicit_override_path=self.llm_override_path,
            agent_type=self.named_agent_type,
        )
        desired_provider = (
            resolved.get("model_provider")
            if resolved and resolved.get("model_provider")
            else (self._configured_model_provider or self.model_provider)
        )
        desired_model_id = (
            resolved.get("model_id")
            if resolved and resolved.get("model_id")
            else (self._configured_model_id or self.model_id)
        )
        configured_model_capabilities = lookup_configured_model_capabilities(
            getattr(self, "model_capabilities", {}) or {},
            provider=desired_provider,
            model_id=desired_model_id,
        )
        override_model_capabilities = lookup_configured_model_capabilities(
            resolved.get("model_capabilities") if resolved else None,
            provider=desired_provider,
            model_id=desired_model_id,
        )
        model_capabilities = {
            **configured_model_capabilities,
            **override_model_capabilities,
        }
        # The tool cap per model reads the override file's map from here, so a
        # model call never reads the file (custom_agents.tool_loading.tool_limit).
        self._persistent_model_capabilities = resolved.get("model_capabilities") if resolved else None
        configured_model_request_constraints = (
            lookup_configured_model_request_constraints(
                getattr(self, "model_capabilities", {}) or {},
                provider=desired_provider,
                model_id=desired_model_id,
            )
        )
        override_model_request_constraints = (
            lookup_configured_model_request_constraints(
                resolved.get("model_capabilities") if resolved else None,
                provider=desired_provider,
                model_id=desired_model_id,
            )
        )
        overrides: dict[str, Any] = {
            "provider": desired_provider,
            "model_id": desired_model_id,
        }
        for field in MODEL_TUNING_FIELDS:
            if resolved and field in resolved:
                overrides[field] = resolved[field]
        for field in MODEL_CAPABILITY_FIELDS:
            if resolved and field in resolved:
                overrides[field] = resolved[field]
            elif field in model_capabilities:
                overrides[field] = model_capabilities[field]
        overrides.update(configured_model_request_constraints)
        overrides.update(override_model_request_constraints)
        profile = runtime_llm_profile_from_agent(
            self,
            overrides=overrides,
            reset_model_specific_fields=(
                desired_provider != self.model_provider
                or desired_model_id != self.model_id
            ),
        )
        self.apply_runtime_llm_profile(
            profile,
            source="persistent_override_file",
        )

    def refresh_runtime_llm_selection(self) -> None:
        model_failover_runtime.refresh_runtime_llm_selection(self)

    def _model_failover_retry_strategy_config(self) -> Optional[Dict[str, int]]:
        return model_failover_runtime.retry_strategy_config(self)

    def _record_model_invocation_success(self, *, task_id: Optional[str]) -> None:
        model_failover_runtime.record_model_success(self, task_id=task_id)

    def _refresh_skills_plugins(self, force: bool = False) -> None:
        """Refresh filesystem-backed Strands skills when the underlying directory changes."""
        if not self.plugins:
            return

        for plugin in self.plugins:
            if not is_agent_skills_plugin(plugin):
                continue
            self._skills_fingerprint = refresh_skills_plugin(
                plugin,
                skills_dir=self.skills_dir,
                additional_skill_directories=self.additional_skill_directories,
                excluded_skill_names=self.skill_exclusions,
                prompt_skill_description_limit=self.prompt_skill_description_limit,
                force=force,
                previous_fingerprint=self._skills_fingerprint,
            )

    def _build_turn_summary(self, task: Task) -> str:
        """Compatibility wrapper for conversation-turn summarization."""
        return build_turn_summary(task)

    async def _construct_header(
        self,
        *,
        comms_message: CommsMessage,
        explicit_to_path: Optional[Union[str, RoutingType]] = None,
        route_response_to_self: bool = False,
    ) -> MessageHeader:
        """Compatibility wrapper for outgoing header construction."""
        return await construct_header(
            comms_message=comms_message,
            explicit_to_path=explicit_to_path,
            route_response_to_self=route_response_to_self,
            agent_id=self.id,
            agent_name=self.name,
            team_id=self.team_id,
            named_agent_type_queue_map=self.named_agent_type_queue_map,
            agent_id_by_name=self._agent_id_by_name,
            logger=self._logger,
            repair_recorder=self._record_runtime_repair_anomaly,
        )

    def _build_conversation_prompt_context(
        self, task_request: Task, msg_meta: MessageMeta
    ) -> str:
        """Compatibility wrapper for conversation prompt context assembly."""
        return build_conversation_prompt_context(
            conversation_store=self._conversation_store,
            conversation_compactor=self._conversation_compactor,
            task_request=task_request,
            msg_meta=msg_meta,
        )

    def _embed_text(self, text: str) -> str:
        if self._embedding_client is None:
            raise RuntimeError("Embedding client is not initialized.")
        if self.memory_graph_enabled and not self._memory_graph_available:
            reason = (
                self._memory_graph_degraded_reason or "embedding backend is unavailable"
            )
            raise RuntimeError(
                f"Memory graph embeddings are disabled for this agent run: {reason}"
            )
        return self._embedding_client.embed_text(text)

    async def _finalize_conversation_turn(
        self, task_info: TaskInfo, task_status: str
    ) -> None:
        """Commit final conversation data in the task owner's storage lane."""
        await finalize_conversation_turn(
            conversation_store=self._conversation_store,
            conversation_compactor=self._conversation_compactor,
            conversation_artifact_offloader=self._conversation_artifact_offloader,
            task_manager=self.task_manager,
            logger=self._logger,
            agent_id=self.id,
            team_id=self.team_id,
            named_agent_type=self.named_agent_type,
            summary_generator=self._conversation_summary_generator,
            strands_messages=list(getattr(self._strands_agent, "messages", []) or []),
            task_info=task_info,
            task_status=task_status,
        )
        await self._notify_context_after_turn(
            task_info=task_info, task_status=task_status
        )

    async def _notify_context_after_turn(
        self, *, task_info: TaskInfo, task_status: str
    ) -> None:
        if self._context_engine is None:
            return
        try:
            await self.task_manager.operations_runtime.run(
                self._context_engine.after_turn,
                TurnFinalizationRequest(
                    task_info=task_info,
                    task_status=task_status,
                    strands_messages=list(
                        getattr(self._strands_agent, "messages", []) or []
                    ),
                ),
            )
        except Exception as exc:
            self._logger.warning(
                "[%s] ContextEngine after_turn lifecycle hook failed for task %s: %s",
                self.id,
                getattr(task_info.task, "task_id", "unknown"),
                exc,
            )

    def _dispatch_memory_review_job(self, review_job: Any) -> None:
        # Memory maintenance commits on the storage worker. Dispatch and all
        # reads of the agent's routing state stay on the owning event loop.
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            owner_loop = self.task_manager.operations_runtime.loop
            if owner_loop is not None and owner_loop.is_running():
                owner_loop.call_soon_threadsafe(
                    self._dispatch_memory_review_job, review_job
                )
                return
        if getattr(review_job, "plugin_id", None) != "pinned_memory_review":
            return
        if self.named_agent_type == MEM_REVIEW_AGENT_TYPE:
            return
        payload = dict(getattr(review_job, "payload", None) or {})
        if not payload.get("persisted"):
            return
        routing_key = self.named_agent_type_queue_map.get(MEM_REVIEW_AGENT_TYPE)
        if not routing_key:
            routing_key = f"{self.team_id}_{MEM_REVIEW_AGENT_TYPE}"
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            self._logger.warning(
                "[%s] Skipping memory review task dispatch for %s because no running event loop is available.",
                self.id,
                getattr(review_job, "job_id", None),
            )
            return
        # The job is saved as queued before this send. If shutdown stops the
        # send, the person's next stale-conversation review still lists the
        # job (scheduled_runtime.prepare_memory_review_schedule).
        start_owned_task(
            self,
            partial(self._publish_memory_review_task, review_job, payload, routing_key),
            name=f"memory-review-dispatch:{getattr(review_job, 'job_id', None)}",
        )

    def _memory_review_task_ttl_seconds(self) -> Optional[float]:
        default_ttl = (
            (AGENT_DEFAULTS.memory.value.get("review") or {}).get(
                "review_task_ttl_seconds"
            )
        ) or 14400
        review_config = {}
        if isinstance(self.memory_config, dict):
            review_config = dict(self.memory_config.get("review") or {})
        raw_ttl = review_config.get("review_task_ttl_seconds", default_ttl)
        if raw_ttl in (None, ""):
            return None
        try:
            ttl_seconds = float(raw_ttl)
        except (TypeError, ValueError):
            self._logger.warning(
                "[%s] Invalid agent.memory.review.review_task_ttl_seconds=%r; using default %s.",
                self.id,
                raw_ttl,
                default_ttl,
            )
            ttl_seconds = float(default_ttl)
        if ttl_seconds <= 0:
            return None
        return ttl_seconds

    async def _publish_memory_review_task(
        self, review_job: Any, payload: dict[str, Any], routing_key: str
    ) -> None:
        if not await self._verify_delegation_target(MEM_REVIEW_AGENT_TYPE):
            self._logger.warning(
                "[%s] Skipping memory review task dispatch for %s because no %s worker "
                "could be verified or started. plugin_id=%s routing_key=%s",
                self.id,
                getattr(review_job, "job_id", None),
                MEM_REVIEW_AGENT_TYPE,
                getattr(review_job, "plugin_id", None),
                routing_key,
            )
            return

        gateway_context = None
        raw_gateway_context = payload.get("gateway_context")
        if isinstance(raw_gateway_context, dict):
            try:
                gateway_context = GatewayContext.model_validate(raw_gateway_context)
            except Exception:
                gateway_context = None
        notification_instruction = memory_review_notification_instruction(
            self.memory_config
        )
        task = Task(
            task_type=MessageMetaType.REQUEST,
            problem_statement=(
                "Review the queued memory job below. For conversation_window jobs, inspect the sliding "
                "window, use overlap turns only as context, create or replace coremem/usermem only for "
                "durable facts, and add per-turn topic tags with topic_tags(action='tag_turn') when useful. For coremem_audit "
                "or usermem_audit jobs, inspect all supplied facts for validity, conflicts, and overlap; "
                "replace or delete outdated facts through the memory tools so the event archive preserves "
                f"the prior fact text and UTC validity dates. {notification_instruction} "
                "Mark the review job complete, skipped, or failed when done.\n\n"
                f"Memory review job:\n{json.dumps(payload, indent=2, sort_keys=True)}"
            ),
        )
        envelope = MessageEnvelope(
            msg_content=task,
            msg_meta=MessageMeta(
                from_agent_name=self.name,
                to_named_agent_type=MEM_REVIEW_AGENT_TYPE,
                msg_correlation_id=new_id("memory_review"),
                conversation_id=payload.get("conversation_id"),
                followup_to_task_id=payload.get("task_id"),
                requester_type=RequesterType.AGENT_REQUESTER,
                is_response_requested=False,
                gateway_context=gateway_context,
            ),
            msg_header=MessageHeader(
                from_agent_id=self.id,
                to_routing_path=routing_key,
                agent_signature="",
            ),
        )
        try:
            ttl_seconds = self._memory_review_task_ttl_seconds()
            await self.team_comms.send_message(
                message=envelope,
                routing_key=routing_key,
                ttl_seconds=ttl_seconds,
            )
            self._logger.info(
                "[%s] Dispatched memory review job %s to %s via %s ttl_seconds=%s.",
                self.id,
                getattr(review_job, "job_id", None),
                MEM_REVIEW_AGENT_TYPE,
                routing_key,
                ttl_seconds,
            )
        except Exception as exc:
            self._logger.warning(
                "[%s] Failed to dispatch memory review job %s to %s: %s",
                self.id,
                getattr(review_job, "job_id", None),
                MEM_REVIEW_AGENT_TYPE,
                exc,
            )

    def _conversation_summary_config(self) -> ConversationSummaryConfig:
        return ConversationSummaryConfig(
            summary_model_provider=self.compaction_model_provider,
            summary_model_id=self.compaction_model_id,
            default_model_provider=self.model_provider,
            default_model_id=self.model_id,
            temperature=self.temperature,
            litellm_api_base=self.litellm_api_base,
            litellm_api_mode=self.litellm_api_mode,
            openai_codex=self.openai_codex,
            xai_oauth=self.xai_oauth,
            instance_root=infer_instance_root(self),
        )

    def _configure_model(self):
        """Configures and returns the appropriate Strands model based on provider."""
        return self._configure_model_for_profile(runtime_llm_profile_from_agent(self))

    def _configure_model_for_profile(
        self, profile: RuntimeLLMProfile, *, request_settings: Mapping[str, Any] | None = None
    ):
        """Build a Strands model for a candidate profile without mutating live state."""
        # Instance-baseline tests use the candidate configuration's endpoint
        # and request options, rather than the lead's possibly different ones.
        request_fields = (
            "temperature", "max_output_tokens", "stream", "request_timeout_seconds",
            "stream_idle_timeout_seconds", "model_call_timeout_seconds",
            "reasoning_trace_retention_messages", "litellm_api_base", "openai_codex", "xai_oauth",
        )
        request_values = {
            field: getattr(self, field) if request_settings is None else request_settings[field]
            for field in request_fields
        }
        return build_strands_model(
            StrandsModelFactoryConfig(
                provider=profile.provider,
                model_id=profile.model_id,
                reasoning_effort=profile.reasoning_effort,
                reasoning_summary=profile.reasoning_summary,
                context_window_tokens=profile.context_window_tokens,
                litellm_api_mode=profile.litellm_api_mode,
                **request_values,
                instance_root=infer_instance_root(self),
            )
        )

    def _strands_model_metadata_config(self) -> Dict[str, Any]:
        return {"context_window_limit": self._resolved_context_window_tokens()}

    def _reset_llm_conversation(self):
        """
        Clear Strands conversation and provider state before each new interaction.

        The task manager carries durable execution history, so retaining the
        provider conversation across separate invocations mostly adds stale tool
        results and can poison later turns with oversized context. Clear any
        transient provider metadata at the same boundary so a future adapter
        cannot accidentally carry request state into an unrelated task.
        """
        if not self._strands_agent:
            return
        messages = getattr(self._strands_agent, "messages", None)
        if hasattr(messages, "clear"):
            messages.clear()
        model_state = getattr(self._strands_agent, "_model_state", None)
        if isinstance(model_state, dict):
            model_state.clear()

    @staticmethod
    def _parsed_comms_routing_snapshot(
        comms_message: CommsMessage, header: MessageHeader
    ) -> Dict[str, Any]:
        return llm_turn_runtime._parsed_comms_routing_snapshot(comms_message, header)

    async def _dispatch_message(
        self,
        msg_header: MessageHeader,
        msg_content: ContentType,
        msg_meta: MessageMeta,
        *,
        message_handled_at: Optional[str] = None,
        source_queue_name: Optional[str] = None,
    ) -> None:
        if isinstance(msg_content, ControlInstruction):
            await self.handle_control_message(
                control_instruction=msg_content,
                msg_header=msg_header,
                msg_meta=msg_meta,
            )
        elif isinstance(msg_content, AgentCreationRequest):
            self.handle_agent_creation_request(
                msg_content, msg_header=msg_header, msg_meta=msg_meta
            )
        elif isinstance(msg_content, AgentStatus):
            await self.handle_info_req_message(
                info_request=msg_content, msg_header=msg_header, msg_meta=msg_meta
            )
        elif isinstance(msg_content, TaskStatus):
            await self._handle_task_status_message(
                task_status=msg_content, msg_header=msg_header, msg_meta=msg_meta
            )
        elif isinstance(msg_content, TaskProgressStatus):
            await handle_task_progress_status(
                self,
                request=msg_content,
                msg_header=msg_header,
                msg_meta=msg_meta,
            )
        elif isinstance(msg_content, ManagerScheduleCommand):
            await handle_manager_schedule_command(
                self,
                command=msg_content,
                msg_header=msg_header,
                msg_meta=msg_meta,
            )
        elif isinstance(msg_content, ManagerRuntimeWorkCommand):
            await handle_manager_runtime_work_command(
                self,
                command=msg_content,
                msg_header=msg_header,
                msg_meta=msg_meta,
            )
        elif isinstance(msg_content, ManagerMemoryCommand):
            await handle_manager_memory_command(
                self,
                command=msg_content,
                msg_header=msg_header,
                msg_meta=msg_meta,
            )
        elif isinstance(msg_content, ManagerKnowledgeCommand):
            from custom_agents.knowledge_command_runtime import handle_manager_knowledge_command

            await handle_manager_knowledge_command(
                self,
                command=msg_content,
                msg_header=msg_header,
                msg_meta=msg_meta,
            )
        elif isinstance(msg_content, PersonStandingCommandV1):
            from custom_agents.person_departure_runtime import handle_person_standing_command

            await handle_person_standing_command(
                self,
                command=msg_content,
                msg_header=msg_header,
                msg_meta=msg_meta,
            )
        elif isinstance(msg_content, ManagerTopicCommand):
            await handle_manager_topic_command(
                self,
                command=msg_content,
                msg_header=msg_header,
                msg_meta=msg_meta,
            )
        elif isinstance(msg_content, TaskSteering):
            await handle_task_steering_message(
                self,
                steering=msg_content,
                msg_header=msg_header,
                msg_meta=msg_meta,
            )
        elif isinstance(msg_content, AfterActionReport):
            await self._handle_after_action_report(
                report=msg_content, msg_header=msg_header, msg_meta=msg_meta
            )
        elif isinstance(msg_content, WorkerTaskHandbackV1):
            await handle_worker_task_handback(
                self,
                handback=msg_content,
                msg_header=msg_header,
                msg_meta=msg_meta,
            )
        elif isinstance(msg_content, ManagedSkillCatalogChangedV1):
            await handle_managed_skill_catalog_notification(
                self,
                notification=msg_content,
                msg_header=msg_header,
                msg_meta=msg_meta,
            )
        elif isinstance(msg_content, DelegatedTaskClaimedV1):
            from custom_agents.delegated_work_runtime import handle_delegated_task_claimed

            await handle_delegated_task_claimed(
                self,
                claim=msg_content,
                msg_header=msg_header,
                msg_meta=msg_meta,
            )
        elif isinstance(msg_content, AccessRequestResolvedV1):
            await handle_access_request_resolved(
                self,
                message=msg_content,
                msg_header=msg_header,
                msg_meta=msg_meta,
            )
        elif isinstance(msg_content, AutomationMessage):
            await handle_automation_message(
                self,
                automation_message=msg_content,
                msg_header=msg_header,
                msg_meta=msg_meta,
            )
        elif isinstance(msg_content, DeliveryReceipt):
            await self._handle_delivery_receipt(
                receipt=msg_content, msg_header=msg_header, msg_meta=msg_meta
            )
        elif isinstance(msg_content, Task):
            if msg_content.task_type is MessageMetaType.RESPONSE:
                await self._handle_task_response(
                    sub_task_response=msg_content,
                    msg_header=msg_header,
                    msg_meta=msg_meta,
                )
            elif msg_content.task_type == MessageMetaType.REQUEST:
                await self._handle_task_request(
                    task_request=msg_content,
                    msg_header=msg_header,
                    msg_meta=msg_meta,
                    message_handled_at=message_handled_at,
                    source_queue_name=source_queue_name,
                )
            else:
                self._logger.warning(
                    f"[{self.id}] Received Task message with unknown task_type: {msg_content.task_type}"
                )
        elif isinstance(msg_content, TaskQuery):
            await self._handle_task_query_message(
                task_query_msg=msg_content,
                incoming_msg_header=msg_header,
                incoming_msg_meta=msg_meta,
            )
        elif isinstance(msg_content, SecureCredentialCollectionResult):
            await self._handle_secure_credential_collection_result(
                credential_result=msg_content,
                incoming_msg_header=msg_header,
                incoming_msg_meta=msg_meta,
            )
        elif isinstance(msg_content, ToolSetupResultV1):
            await self._handle_tool_setup_result(
                setup_result=msg_content,
                incoming_msg_header=msg_header,
                incoming_msg_meta=msg_meta,
            )
        elif isinstance(msg_content, ScheduledMessage):
            await self._handle_scheduled_message(
                msg_content, msg_header=msg_header, msg_meta=msg_meta
            )
        elif isinstance(
            msg_content, (SecureCredentialCollectionRequest, SignupAutomationRequest)
        ):
            # Only the gateway opens forms; a request reaching an agent comes
            # from a delegated agent and is answered with a refusal.
            await task_message_runtime.refuse_credential_request_from_delegate(
                self, msg_content, msg_header, msg_meta
            )
        else:
            self._logger.warning(
                f"[{self.id}] Unhandled message content type: {type(msg_content).__name__}"
            )

    def _is_priority_deferred_message(self, envelope: MessageEnvelope) -> bool:
        return is_priority_deferred_message(envelope)

    def _requires_execution_slot(self, envelope: MessageEnvelope) -> bool:
        return requires_execution_slot(envelope)

    @staticmethod
    def _is_operational_content(content: ContentType) -> bool:
        return isinstance(
            content,
            (
                AgentStatus,
                ControlInstruction,
                ManagedSkillCatalogChangedV1,
                ManagerRuntimeWorkCommand,
                TaskProgressStatus,
                TaskStatus,
                TaskSteering,
            ),
        )

    def _operational_target_matches(
        self,
        *,
        msg_content: ContentType,
        msg_header: MessageHeader,
        msg_meta: MessageMeta,
    ) -> bool:
        target_agent_id = (
            getattr(msg_content, "target_agent_id", None) or msg_header.to_routing_path
        )
        if target_agent_id in {
            None,
            "",
            RoutingType.ALL_AGENTS_ROUTE.value,
            RoutingType.ALL_AGENTS_ROUTE,
        }:
            target_agent_id = None
        target_agent_name = getattr(msg_content, "target_agent_name", None) or getattr(
            msg_meta, "to_agent_name", None
        )
        target_named_agent_type = getattr(
            msg_content, "target_named_agent_type", None
        ) or getattr(msg_meta, "to_named_agent_type", None)

        if target_agent_id and str(target_agent_id) != self.id:
            return False
        if target_agent_name and str(target_agent_name) != self.name:
            return False
        if target_named_agent_type and str(target_named_agent_type) != str(
            self.named_agent_type or ""
        ):
            return False

        if isinstance(msg_content, TaskStatus):
            return bool(
                target_agent_id or target_named_agent_type or target_agent_name
            ) or (self.task_manager.get_task_info(msg_content.task_id) is not None)
        if isinstance(msg_content, TaskSteering):
            target_task_id = getattr(msg_content, "target_task_id", None)
            target_correlation_id = getattr(msg_content, "target_correlation_id", None)
            if (
                target_task_id
                and self.task_manager.get_task_info(target_task_id) is not None
            ):
                return True
            if (
                target_correlation_id
                and self.task_manager.find_task_info_by_correlation_id(
                    target_correlation_id
                )
                is not None
            ):
                return True
            return bool(target_agent_id or target_named_agent_type or target_agent_name)
        if isinstance(msg_content, ControlInstruction) and msg_content.target_task_id:
            if target_agent_id or target_agent_name or target_named_agent_type:
                return True
            return (
                self.task_manager.get_task_info(msg_content.target_task_id) is not None
            )
        return bool(
            target_agent_id
            or target_agent_name
            or target_named_agent_type
            or isinstance(
                msg_content,
                (AgentStatus, ManagedSkillCatalogChangedV1),
            )
        )

    async def _send_operational_message(self, message: MessageEnvelope) -> None:
        if not self.team_id:
            raise RuntimeError("Operational fanout requires a team_id.")
        await self._ensure_nats_transport_adapter().publish(
            envelope=message,
            routing_key=str(
                message.msg_header.to_routing_path or RoutingType.ALL_AGENTS_ROUTE.value
            ),
            options=TransportPublishOptions(
                durable=False,
                operational=True,
                idempotency_key=message.msg_header.idempotency_key
                or message.msg_header.message_id,
            ),
        )

    async def _handle_task_status_message(
        self,
        task_status: TaskStatus,
        msg_header: MessageHeader,
        msg_meta: MessageMeta,
    ) -> None:
        if task_status.status_type == MessageMetaType.RESPONSE:
            future = self._pending_task_status_futures.get(task_status.task_status_id)
            if (
                task_status.task_status_id in self._known_only_task_status_probes
                and not task_status.is_known_task
            ):
                # Another worker of the type answered that it does not hold the
                # task; wait for the one that does, or the probe's timeout.
                return
            if future is not None and not future.done():
                future.set_result(task_status)
            return

        if not self._operational_target_matches(
            msg_content=task_status,
            msg_header=msg_header,
            msg_meta=msg_meta,
        ):
            return

        response_key = msg_header.response_routing_key
        if not response_key:
            self._logger.warning(
                f"[{self.id}] TaskStatus request {task_status.task_status_id} had no response routing key."
            )
            return

        task_info = await self.task_manager.operations_runtime.run(
            self.task_manager.get_task_info, task_status.task_id
        )
        last_task_update_at = (
            task_info.task.task_state[-1][1]
            if task_info is not None and task_info.task.task_state
            else None
        )
        response = TaskStatus(
            task_status_id=task_status.task_status_id,
            status_type=MessageMetaType.RESPONSE,
            task_id=task_status.task_id,
            parent_task_id=(
                task_status.parent_task_id
                or (task_info.task.parent_task_id if task_info is not None else None)
            ),
            target_agent_id=task_status.target_agent_id,
            target_agent_name=task_status.target_agent_name,
            target_named_agent_type=task_status.target_named_agent_type,
            correlation_id=task_status.correlation_id,
            responder_agent_id=self.id,
            responder_agent_name=self.name,
            responder_named_agent_type=self.named_agent_type,
            is_known_task=task_info is not None,
            is_current_task=(
                task_info is not None
                and task_status.task_id == self.task_manager.current_task_id
            ),
            task_state=task_info.state if task_info is not None else None,
            last_task_update_at=last_task_update_at,
            is_executing=self.is_executing,
            is_exiting=self._should_exit.is_set(),
            status_text=(
                "Task is known by this agent."
                if task_info is not None
                else "Task is not known by this agent."
            ),
        )
        envelope = MessageEnvelope(
            msg_content=response,
            msg_header=MessageHeader(
                from_agent_id=self.id,
                to_routing_path=response_key,
                agent_signature="",
            ),
            msg_meta=MessageMeta(
                from_agent_name=self.name,
                msg_correlation_id=msg_meta.msg_correlation_id
                or task_status.correlation_id,
                conversation_id=getattr(msg_meta, "conversation_id", None),
            ),
        )
        await self._send_operational_message(envelope)

    def _turn_action_from_outbox_record(self, record: Any) -> TurnAction:
        action_payload = dict(record.payload_json.get("action") or {})
        decision = StructuredAgentTurnDecision.model_validate(
            {"actions": [action_payload]}
        )
        return decision.actions[0]

    async def _ensure_turn_action_source_task(
        self,
        *,
        source_task_id: Optional[str],
        incoming_content: ContentType,
        incoming_header: MessageHeader,
        incoming_meta: MessageMeta,
    ) -> bool:
        """Ensure direct process_llm_interaction callers still get a durable outbox source."""
        if not source_task_id:
            return False
        if (
            await self.task_manager.operations_runtime.run(
                self.task_manager.get_task_info, source_task_id
            )
            is not None
        ):
            return True

        if isinstance(incoming_content, Task):
            source_task = incoming_content
        else:
            source_task = Task(
                task_id=source_task_id,
                task_type=MessageMetaType.REQUEST,
                problem_statement=(
                    f"Runtime source placeholder for {incoming_content.__class__.__name__} "
                    f"{incoming_content.get_id()}."
                ),
            )

        # Admitted like any task: its owner comes from verified identity only.
        owner_person_id = await asyncio.to_thread(verified_meta_person_id, self, incoming_meta)
        accepted = await self.task_manager.operations_runtime.run(
            self.task_manager.add_task, source_task, incoming_header, incoming_meta,
            owner_person_id=owner_person_id,
        )
        if not accepted:
            return False

        queued_task_id = await self.task_manager.operations_runtime.run(
            self.task_manager.get_next_task_to_process,
        )
        if queued_task_id and queued_task_id != source_task_id:
            self._logger.warning(
                "[%s] Direct runtime action source registration dequeued unexpected task %s while preparing %s.",
                self.id,
                queued_task_id,
                source_task_id,
            )
        await self.task_manager.operations_runtime.run(
            self.task_manager.update_main_task_state,
            source_task_id,
            ProgressState.IN_PROGRESS,
            datetime.now(timezone.utc).isoformat(),
        )
        await self.task_manager.operations_runtime.run(
            self.task_manager.append_history_entry,
            source_task_id,
            "Registered direct process_llm_interaction source task for runtime action outbox execution.",
            entry_type=TaskHistoryType.PLAN_EVENT,
            importance=TaskHistoryImportance.NORMAL,
            agent_name=self.name,
            agent_id=self.id,
            details={"event": "direct_invocation_outbox_source_registered"},
        )
        return True

    async def _drain_due_turn_action_outbox(self) -> bool:
        await self.task_manager.operations_runtime.run(
            self.task_manager.mark_stale_dispatching_turn_actions,
            stale_after_seconds=self._turn_action_outbox_float(
                "dispatching_stale_after_seconds", 30.0
            ),
        )
        due_records = await self.task_manager.operations_runtime.run(
            self.task_manager.due_turn_action_retry_records,
        )
        if not due_records:
            return False

        for due_record in due_records:
            batch = await self.task_manager.operations_runtime.run(
                self.task_manager.get_turn_action_batch, due_record.batch_id
            )
            if batch is None:
                continue
            source_info = await self.task_manager.operations_runtime.run(
                self.task_manager.get_task_info, due_record.source_task_id
            )
            if source_info is None:
                await self.task_manager.operations_runtime.run(
                    self.task_manager.mark_turn_action_record,
                    due_record.action_id,
                    status="failed_terminal",
                    recoverability="terminal",
                    last_error="Source task is no longer active.",
                )
                continue
            await self.task_manager.operations_runtime.run(
                self.task_manager.mark_turn_action_batch_status,
                batch.batch_id,
                "retrying",
                current_action_index=due_record.action_index,
            )
            records = await self.task_manager.operations_runtime.run(
                self.task_manager.list_turn_action_records, batch.batch_id
            )
            root_task_id = (
                await self.task_manager.operations_runtime.run(
                    self.task_manager.resolve_root_task_id, due_record.source_task_id
                )
                or due_record.source_task_id
            )
            coordinator = self._runtime_coordinator()
            lease = None
            try:
                lease = await self.task_manager.operations_runtime.run(
                    coordinator.begin_batch,
                    root_task_id=root_task_id,
                    source_task_id=due_record.source_task_id,
                    batch_id=batch.batch_id,
                )
                for record in records:
                    if record.action_index < due_record.action_index:
                        continue
                    if record.status in {"completed", "dispatched"}:
                        continue
                    if record.status == "blocked":
                        await self.task_manager.operations_runtime.run(
                            self.task_manager.mark_turn_action_record,
                            record.action_id,
                            status="pending",
                        )
                    action = self._turn_action_from_outbox_record(record)
                    await self._execute_turn_action_with_recovery(
                        batch_id=batch.batch_id,
                        record_id=record.action_id,
                        action=action,
                        incoming_content=source_info.task,
                        incoming_header=source_info.header,
                        incoming_meta=source_info.meta,
                    )
                    await self.task_manager.operations_runtime.run(
                        coordinator.interrupt_if_requested,
                        lease.lease_id if lease is not None else None,
                        batch.batch_id,
                        record.action_index,
                    )
                await self.task_manager.operations_runtime.run(
                    self.task_manager.mark_turn_action_batch_status,
                    batch.batch_id,
                    "completed",
                )
                await self.task_manager.operations_runtime.run(
                    coordinator.complete_batch,
                    lease_id=lease.lease_id if lease is not None else None,
                    root_task_id=root_task_id,
                    batch_id=batch.batch_id,
                    status="completed",
                )
                return True
            except TurnActionRecoveryScheduled:
                if lease is not None:
                    await self.task_manager.operations_runtime.run(
                        coordinator.complete_batch,
                        lease_id=lease.lease_id,
                        root_task_id=root_task_id,
                        batch_id=batch.batch_id,
                        # This attempt failed; the outbox owns the pending retry.
                        status="failed",
                    )
                return True
            except RuntimeBatchInterrupted:
                if lease is not None:
                    await self.task_manager.operations_runtime.run(
                        coordinator.complete_batch,
                        lease_id=lease.lease_id,
                        root_task_id=root_task_id,
                        batch_id=batch.batch_id,
                        status="interrupted",
                    )
                return True
            except Exception:
                if lease is not None:
                    await self.task_manager.operations_runtime.run(
                        coordinator.complete_batch,
                        lease_id=lease.lease_id,
                        root_task_id=root_task_id,
                        batch_id=batch.batch_id,
                        status="failed",
                    )
                raise
        return False

    @property
    def deferred_inbox(self) -> DeferredMessageInbox:
        if self._deferred_inbox is None:
            self._deferred_inbox = DeferredMessageInbox(
                owner=DeferredInboxOwner(
                    self.id, self.name, self.team_id, self.named_agent_type
                ),
                repository=self.task_manager.runtime_state_store,
                is_execution_cancelled=lambda task_id: (
                    self.task_manager.operations_runtime.run(
                        self.task_manager.is_execution_cancelled, task_id
                    )
                ),
                logger=self._logger,
                warning_threshold=self.deferred_message_warning_threshold,
                capacity=self.max_deferred_messages,
            )
        return self._deferred_inbox

    async def _dispatch_deferred_envelope(self, envelope: MessageEnvelope) -> None:
        async with self._execution_scope():
            await self._dispatch_message(
                msg_header=envelope.msg_header,
                msg_content=envelope.msg_content,
                msg_meta=envelope.msg_meta,
                message_handled_at=datetime.now(timezone.utc).isoformat(),
                source_queue_name="deferred",
            )

    def _can_continue_pending_work(self) -> bool:
        return not (self.paused or self.is_executing or self._should_exit.is_set())

    async def _continue_pending_work(self) -> bool:
        """Run one drain pass. Only the retained drain calls this.

        ``pending_work_runtime`` keeps that drain in one task, so passes never
        overlap. Nothing new starts once the agent is stopping. Returns False
        when a waiting message failed and was kept, so the drain stops instead
        of retrying it at once.
        """
        while self._can_continue_pending_work():
            if self._pending_interrupt_responses:
                interrupted_task_id = next(iter(self._pending_interrupt_responses))
                await self._resume_interrupted_task_turn(interrupted_task_id)
                continue
            if self.deferred_inbox:
                drained = await self.deferred_inbox.drain(
                    dispatch=self._dispatch_deferred_envelope,
                    available=self._can_continue_pending_work,
                )
                if not drained:
                    return False
                continue
            retried_outbox = await self._drain_due_turn_action_outbox()
            if retried_outbox:
                continue
            drained_runtime_mailbox = (
                await self._runtime_coordinator().drain_all_deferred_events()
            )
            if drained_runtime_mailbox:
                continue
            started = await self._check_and_process_next_task()
            if started:
                continue
            break
        return True

    async def _apply_runtime_content_defaults(
        self,
        generated_content: ContentType,
        incoming_content: ContentType,
    ) -> ContentType:
        """
        Fill reply linkage fields from the inbound message when the model omits them.

        Task and TaskQuery replies often need to preserve task ids or parent-task
        linkage that the runtime already knows. Keeping these defaults in code is
        more reliable than requiring the model to restate them perfectly.
        """
        if isinstance(generated_content, Task):
            if generated_content.task_type == MessageMetaType.RESPONSE and isinstance(
                incoming_content, Task
            ):
                if generated_content.task_id != incoming_content.task_id:
                    await self._record_runtime_repair_anomaly(
                        "task_response_task_id_corrected",
                        "Task response task_id did not match inbound task; runtime corrected the reply linkage.",
                        related_task_id=incoming_content.task_id,
                        details={
                            "generated_task_id": generated_content.task_id,
                            "expected_task_id": incoming_content.task_id,
                            "content_type": "Task",
                        },
                    )
                generated_content.task_id = incoming_content.task_id
                if incoming_content.parent_task_id:
                    if (
                        generated_content.parent_task_id
                        != incoming_content.parent_task_id
                    ):
                        await self._record_runtime_repair_anomaly(
                            "task_response_parent_task_id_corrected",
                            "Task response parent_task_id did not match inbound task; runtime corrected the reply linkage.",
                            related_task_id=incoming_content.task_id,
                            details={
                                "generated_parent_task_id": generated_content.parent_task_id,
                                "expected_parent_task_id": incoming_content.parent_task_id,
                                "content_type": "Task",
                            },
                        )
                    generated_content.parent_task_id = incoming_content.parent_task_id

        elif isinstance(generated_content, TaskQuery):
            if isinstance(incoming_content, Task):
                if not generated_content.parent_task_id:
                    await self._record_runtime_repair_anomaly(
                        "task_query_parent_task_id_filled",
                        "TaskQuery request omitted parent_task_id; runtime filled it from the inbound task.",
                        related_task_id=incoming_content.task_id,
                        details={
                            "task_query_id": generated_content.task_query_id,
                            "expected_parent_task_id": incoming_content.task_id,
                            "query_type": generated_content.query_type.value,
                        },
                    )
                    generated_content.parent_task_id = incoming_content.task_id
            elif isinstance(incoming_content, TaskQuery):
                if generated_content.parent_task_id != incoming_content.parent_task_id:
                    await self._record_runtime_repair_anomaly(
                        "task_query_parent_task_id_corrected",
                        "TaskQuery parent_task_id did not match the inbound query; runtime corrected the reply linkage.",
                        related_task_id=incoming_content.parent_task_id,
                        details={
                            "generated_parent_task_id": generated_content.parent_task_id,
                            "expected_parent_task_id": incoming_content.parent_task_id,
                            "task_query_id": generated_content.task_query_id,
                            "inbound_task_query_id": incoming_content.task_query_id,
                            "query_type": generated_content.query_type.value,
                        },
                    )
                    generated_content.parent_task_id = incoming_content.parent_task_id
                if (
                    generated_content.query_type == MessageMetaType.RESPONSE
                    and generated_content.task_query_id
                    != incoming_content.task_query_id
                ):
                    await self._record_runtime_repair_anomaly(
                        "task_query_response_id_corrected",
                        "TaskQuery response task_query_id did not match the inbound query; runtime corrected it.",
                        related_task_id=incoming_content.parent_task_id,
                        details={
                            "generated_task_query_id": generated_content.task_query_id,
                            "expected_task_query_id": incoming_content.task_query_id,
                            "parent_task_id": incoming_content.parent_task_id,
                        },
                    )
                if generated_content.query_type == MessageMetaType.RESPONSE:
                    generated_content.task_query_id = incoming_content.task_query_id

        elif isinstance(generated_content, SecureCredentialCollectionRequest):
            if (
                isinstance(incoming_content, Task)
                and not generated_content.parent_task_id
            ):
                await self._record_runtime_repair_anomaly(
                    "secure_credential_request_parent_task_id_filled",
                    "Secure credential collection request omitted parent_task_id; runtime filled it from the inbound task.",
                    related_task_id=incoming_content.task_id,
                    details={
                        "credential_request_id": generated_content.credential_request_id,
                        "expected_parent_task_id": incoming_content.task_id,
                    },
                )
                generated_content.parent_task_id = incoming_content.task_id

        elif isinstance(generated_content, SignupAutomationRequest):
            if (
                isinstance(incoming_content, Task)
                and not generated_content.parent_task_id
            ):
                await self._record_runtime_repair_anomaly(
                    "signup_automation_request_parent_task_id_filled",
                    "Signup automation request omitted parent_task_id; runtime filled it from the inbound task.",
                    related_task_id=incoming_content.task_id,
                    details={
                        "signup_request_id": generated_content.signup_request_id,
                        "expected_parent_task_id": incoming_content.task_id,
                    },
                )
                generated_content.parent_task_id = incoming_content.task_id

        return generated_content

    def set_exit_flag(self):
        """Signals the agent's main listening loop to exit."""
        self._should_exit.set()

    def _extract_team_id_from_routing_value(
        self, routing_value: Optional[Union[str, RoutingType]]
    ) -> Optional[str]:
        if routing_value is None or isinstance(routing_value, RoutingType):
            return None
        if routing_value == self.team_id or routing_value.startswith(
            f"{self.team_id}_"
        ):
            return self.team_id
        route = str(routing_value)
        for marker in ("_user_interact_queue", "_gateway"):
            marker_index = route.find(marker)
            if marker_index > 0:
                return route[:marker_index]
        candidate_named_types = set(self.named_agent_type_queue_map.keys())
        if self.named_agent_type:
            candidate_named_types.add(self.named_agent_type)
        for named_type in sorted(candidate_named_types, key=len, reverse=True):
            suffix = f"_{named_type}"
            if named_type and route.endswith(suffix):
                return route[: -len(suffix)]
        if route.startswith("team_"):
            parts = route.split("_")
            if len(parts) >= 2:
                return "_".join(parts[:2])
        return None

    def _message_targets_other_team(self, msg_envelope: MessageEnvelope) -> bool:
        team_candidates = set()
        msg_header = msg_envelope.msg_header
        msg_content = msg_envelope.msg_content

        for routing_value in (
            msg_header.to_routing_path,
            msg_header.response_routing_key,
        ):
            team_id = self._extract_team_id_from_routing_value(routing_value)
            if team_id:
                team_candidates.add(team_id)

        if (
            isinstance(
                msg_content,
                (AgentStatus, AgentCreationRequest, ManagedSkillCatalogChangedV1),
            )
            and msg_content.team_id
            and msg_content.team_id != "NO_TEAM"
        ):
            team_candidates.add(msg_content.team_id)

        if not team_candidates:
            return False
        return self.team_id not in team_candidates

    def handle_agent_creation_request(
        self,
        request: AgentCreationRequest,
        msg_header: Optional[MessageHeader] = None,
        msg_meta: Optional[MessageMeta] = None,
        *, source: Literal["manual", "delegation"] = "manual",
    ):
        from custom_agents.agent_instantiation_service import AgentInstantiationService
        from custom_messages.contracts.schedule_worker_v1 import AgentCreationResultV1, AgentLaunchResultV1

        if not (request.team_id or self.team_id):
            self._logger.error("Agent creation requires a team_id. event=agent_creation_team_id_missing")
            return AgentCreationResultV1(request_id=request.request_id, results=(AgentLaunchResultV1(
                status="denied", reason="agent_creation_team_id_missing"),))
        if msg_header is not None and msg_header.from_agent_id != self.id:
            return AgentCreationResultV1(request_id=request.request_id, results=(AgentLaunchResultV1(
                status="denied", reason="agent_creation_requires_local_lead_action"),))
        return AgentInstantiationService(self).create(request, source=source)

    def get_exit_event(self) -> asyncio.Event:
        """
        Returns the asyncio.Event used to signal the agent's exit.

        Returns:
            asyncio.Event: The event object.
        """
        return self._should_exit

    def _nats_mapper(self) -> NatsSubjectMapper:
        return NatsSubjectMapper(
            team_id=self.team_id or DEFAULT_TEAM_ID,
            subject_prefix=self.nats_subject_prefix,
            named_agent_type_queue_map=self.named_agent_type_queue_map,
            user_queue_name=self.user_input_queue_name,
        )

    def _ensure_nats_transport_adapter(self) -> NatsTransportAdapter:
        adapter = self.team_comms._ensure_nats_adapter(self.team_id)
        adapter.mapper = self._nats_mapper()
        return adapter

    async def _subscribe_type_queue(
        self,
        adapter: NatsTransportAdapter,
        *,
        policy: FetchPolicy,
        ready: Callable[[], bool],
    ) -> None:
        """Read this agent's type queue, shared by every agent of the type (ADR-019)."""

        named_type = str(self.named_agent_type)
        queue_name = self.named_agent_type_queue_map.get(named_type) or f"{self.team_id}_{named_type}"
        await adapter.subscribe_subject(
            name=f"{self.id}:type:{named_type}",
            subject=self._nats_mapper().named_type_work_subject(named_type),
            handler=self.on_nats_delivery,
            durable_name=adapter.worker_queue_durable_name(named_type),
            logical_route=queue_name,
            consumer_role=NAMED_WORKER_CONSUMER_ROLE,
            owner_agent_id=self.id,
            fetch_policy=policy,
            ready=ready,
        )
        self._logger.info(
            "[%s] NATS type queue ready for %s. event=agent_type_queue_ready "
            "agent_type=%s fetch_policy=%s",
            self.id,
            queue_name,
            named_type,
            policy.value,
        )

    async def _connect_nats(self) -> None:
        """Connect using NATS/JetStream."""

        self._runtime_loop = asyncio.get_running_loop()
        if self._activity_client:
            await self._activity_client.start()
        adapter = self._ensure_nats_transport_adapter()
        await adapter.connect(
            reconcile_topology=self.is_team_lead and not self.instance_config_path
        )
        try:
            # A durable subscription can deliver before its setup await returns.
            # Prepare shared schedule state first; start the scheduler only after
            # bootstrap below, and retain any store opened by earlier setup.
            self._ensure_schedule_store()
            # Before any subscription: the lead stores presence as it arrives,
            # including the replay of every agent's last status at subscribe.
            self._agent_state_store = AgentStateStore(
                db_path=self.task_manager._db_path,
                logger=self._logger,
                storage_settings=self.task_manager.storage_settings,
            )
            self._scheduler_wake = asyncio.Event()
            direct_durable = self._nats_mapper().durable_name(f"agent_{self.id}")
            await adapter.subscribe_route(
                name=f"{self.id}:direct",
                routing_key=self.id,
                handler=self.on_nats_delivery,
                durable_name=direct_durable,
                consumer_scope=_direct_consumer_scope(
                    configured_fields=self.model_fields_set
                ),
                consumer_role="agent_direct",
                owner_agent_id=self.id,
                # A mailbox stops asking while its deferred inbox is full.
                ready=lambda: mailbox_has_room(self),
            )
            self._logger.info("[%s] NATS direct route ready for %s.", self.id, self.id)

            await _subscribe_manager_control_route(self, adapter)

            if self.team_id:
                self._operational_exchange_name = (
                    self._nats_mapper().operational_subject()
                )
                await adapter.subscribe_subject(
                    name=f"{self.id}:operational",
                    subject=self._operational_exchange_name,
                    handler=self.on_nats_operational_delivery,
                    logical_route=RoutingType.ALL_AGENTS_ROUTE.value,
                    delivery=SubscriptionDelivery.CORE,
                )
                # Operational requests fan out over one Core NATS subject. Replies
                # target the requesting agent id and are correlated in memory.
                self._operational_queue_name = self.id

                if self.is_team_lead:
                    await adapter.subscribe_route(
                        name=f"{self.id}:lead",
                        routing_key=self.team_id,
                        handler=self.on_nats_delivery,
                        durable_name=self._nats_mapper().durable_name(
                            f"lead_{self.team_id}"
                        ),
                        consumer_role="team_lead",
                        owner_agent_id=self.id,
                        ready=lambda: mailbox_has_room(self),
                    )

                if _should_consume_broadcasts(
                    self.process_broadcasts,
                    is_team_lead=self.is_team_lead,
                ):
                    await adapter.subscribe_subject(
                        name=f"{self.id}:broadcast",
                        subject=f"{self._nats_mapper().prefix}.broadcast",
                        handler=self.on_nats_delivery,
                        logical_route=RoutingType.ALL_AGENTS_ROUTE.value,
                        delivery=SubscriptionDelivery.CORE,
                    )
                    await adapter.subscribe_subject(
                        name=f"{self.id}:presence",
                        subject=f"{self._nats_mapper().prefix}.presence.*",
                        handler=self.on_nats_delivery,
                        logical_route=RoutingType.ALL_AGENTS_ROUTE.value,
                        delivery=SubscriptionDelivery.JETSTREAM_EPHEMERAL,
                        consumer_scope=ConsumerScope.PROCESS,
                        consumer_role="presence_snapshot",
                        owner_agent_id=self.id,
                        deliver_last_per_subject=True,
                    )

            if self.is_team_lead and self.named_agent_type:
                # The lead is the only TeamArchitect; work addressed to its type
                # (manager escalations) is admitted to its own queue like any
                # mailbox message.
                await self._subscribe_type_queue(
                    adapter, policy=FetchPolicy.CONTINUOUS, ready=lambda: mailbox_has_room(self)
                )

            self.named_agent_type_queue_map[
                f"{self.team_id}_{self.user_name_postfix}"
            ] = f"{self.team_id}_{self.user_queue_postfix}"

            if self.team_id:
                await self._publish_presence_status(is_exiting=False)
                self._presence_task = asyncio.create_task(
                    self._presence_heartbeat_loop(),
                    name=f"{self.id}:presence_heartbeat",
                )

            if self.is_team_lead:
                try:
                    run_manager_bootstrap(self)
                except Exception:
                    self._logger.exception(
                        "[%s] Manager-owned bootstrap failed. event=manager_bootstrap_failed",
                        self.id,
                    )
            if self.named_agent_type == INTERNAL_IMPROVEMENT_AGENT_TYPE:
                await publish_internal_improvement_schedule_ensure(self)
            await self._refresh_self_agent_state()

            # Before recovery, which can end tasks: their delegated work stops too,
            # and the records of tasks this agent delegated close with them.
            from custom_agents.delegated_work_runtime import (
                register_delegated_task_endings,
                report_held_delegated_tasks,
            )
            from custom_agents.orphaned_subtasks import register_parent_end_stops

            register_parent_end_stops(self)
            register_delegated_task_endings(self)
            install_custody_listeners(self, interrupt_turn=_interrupt_active_execution)
            await self._recover_runtime_after_restart()
            # Delegated tasks this agent holds after a restart, or took over
            # from a worker that is gone: tell each requester who holds them.
            await report_held_delegated_tasks(self)
            if self.is_team_lead:
                # Safety net: end the work of anyone disabled or removed while
                # this manager was stopped (custom_agents.person_departure_runtime).
                from custom_agents.person_departure_runtime import settle_departed_people_safely

                await settle_departed_people_safely(self, when="manager_start")
            from custom_agents.agent_instantiation_service import update_runtime_launch
            from custom_messages.contracts.schedule_worker_v1 import AgentLaunchState

            update_runtime_launch(self, state=AgentLaunchState.READY)
            self._worker_ready = True
            if not self.is_team_lead and self.named_agent_type:
                # Only now, after recovery: a worker asks its type's shared
                # queue for one task at a time, and only while it is free.
                await self._subscribe_type_queue(
                    adapter, policy=FetchPolicy.WHEN_READY, ready=lambda: may_take_pool_task(self)
                )
            from custom_agents.agent_team_runtime import register_local_worker
            from custom_agents.schedule_worker_controller import local_schedule_runtime_loop

            register_local_worker(self)
            if self.is_team_lead and self._local_team_runtime is not None:
                self._local_schedule_task = asyncio.create_task(local_schedule_runtime_loop(self))
            self._scheduler_task = asyncio.create_task(self._scheduler_loop())
            # Work recovered above runs in the retained drain, so a stop
            # request during a resumed turn still reaches cleanup.
            schedule_pending_work(self)

            self._logger.info(
                "[%s] NATS transport ready; waiting for messages until exit.", self.id
            )
            await self._should_exit.wait()
        finally:
            await self._cleanup_nats_runtime_on_exit(adapter)
            if self._activity_client:
                await self._activity_client.close()
            self._runtime_loop = None

    def _cancel_scheduler_pass(self) -> None:
        """Stop the scheduler's current pass; cleanup joins it.

        A signal exit stops it together with the drain, as exit_agent does, so
        it cannot claim or queue work while the drain is joined.
        """
        if self._scheduler_task is not None:
            self._scheduler_task.cancel()

    async def _cleanup_nats_runtime_on_exit(
        self, adapter: NatsTransportAdapter
    ) -> None:
        self._cancel_scheduler_pass()
        await close_pending_work(self)
        unregister_local_worker(self.id)
        if self._local_schedule_task is not None:
            self._local_schedule_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._local_schedule_task
            self._local_schedule_task = None
        if self._presence_task and not self._presence_task.done():
            self._presence_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._presence_task
            self._presence_task = None

        if self._scheduler_task and not self._scheduler_task.done():
            self._scheduler_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._scheduler_task

        if self.team_id:
            try:
                await _publish_exit_presence(self)
            except Exception as exc:
                self._logger.warning(
                    "[%s] Failed to publish NATS exit presence: %s", self.id, exc
                )

        # Delivery callbacks can still be committing task state while shutdown
        # starts. Join them before releasing their provider and state resources.
        await adapter.close()

        # Every agent, the lead included, writes only its own status row as it
        # stops. Once the exit flag is set every refresh already marks the row
        # exiting, and the exit presence went out above; this last write comes
        # after deliveries settled, so the row ends exiting whatever ran before.
        # The lead used to delete the whole team's rows here, so startup agents
        # that keep running through a manager-only restart looked gone; old rows
        # are now removed by agent_liveness.prune_departed_agent_rows.
        await self._refresh_self_agent_state(is_exiting=True)

    async def _publish_presence_status(self, *, is_exiting: bool) -> None:
        if not self.team_id:
            return
        message = MessageEnvelope(
            msg_content=self.get_self_status(is_exiting=is_exiting),
            msg_meta=MessageMeta(from_agent_name=self.name, to_named_agent_type=None),
            msg_header=MessageHeader(
                from_agent_id=self.id,
                to_routing_path=RoutingType.ALL_AGENTS_ROUTE,
                agent_signature="",
            ),
        )
        await self.team_comms.broadcast_message(
            message=message,
            team_id=self.team_id,
        )

    async def _presence_heartbeat_loop(self) -> None:
        while not self._should_exit.is_set():
            await asyncio.sleep(NATS_PRESENCE_HEARTBEAT_SECONDS)
            if self._should_exit.is_set():
                return
            try:
                await repair_agent_skill_catalog(
                    self,
                    reason="presence_heartbeat",
                )
                await self._publish_presence_status(is_exiting=False)
                await self._refresh_self_agent_state()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self._logger.warning(
                    "[%s] Failed to publish NATS presence heartbeat: %s", self.id, exc
                )

    async def connect(self):
        """Connect to NATS/JetStream and consume messages until exit."""
        await self._connect_nats()

    async def on_nats_delivery(self, delivery: InboundDelivery) -> None:
        """Process a structured message delivered by the NATS transport adapter."""

        msg_envelope = delivery.envelope
        msg_header = msg_envelope.msg_header
        msg_content = msg_envelope.msg_content
        msg_meta = msg_envelope.msg_meta or MessageMeta()

        if msg_header.from_agent_id == self.id:
            # An agent never works on a message it sent itself.
            await delivery.ack()
            return

        if self._message_targets_other_team(msg_envelope):
            self._logger.warning(
                "[%s] Ignoring NATS message %s intended for another team. Current team=%s route=%s.",
                self.id,
                msg_header.message_id,
                self.team_id,
                delivery.logical_route,
            )
            await delivery.ack()
            return

        effective_to_routing_path = msg_header.to_routing_path or delivery.logical_route
        is_for_all = effective_to_routing_path == RoutingType.ALL_AGENTS_ROUTE.value
        is_for_me = (
            effective_to_routing_path == self.id or delivery.logical_route == self.id
        )
        is_for_my_type = (
            effective_to_routing_path in self.named_agent_type_queue_map.values()
            or delivery.logical_route in self.named_agent_type_queue_map.values()
        )
        # The durable lead subscription uses the team id, not a process id or
        # named-type queue. Accept it only on that subscription for this lead.
        is_for_team_lead = (
            effective_to_routing_path == self.team_id
            and delivery.logical_route == self.team_id
            and self.is_team_lead
        )

        if is_for_all and self._should_apply_stale_broadcast_filter(msg_envelope):
            if msg_header.timestamp:
                try:
                    msg_time = datetime.fromisoformat(msg_header.timestamp)
                    if msg_time.tzinfo is None:
                        msg_time = msg_time.replace(tzinfo=timezone.utc)
                    age_ms = (
                        datetime.now(timezone.utc) - msg_time
                    ).total_seconds() * 1000.0
                    if age_ms > self.max_broadcast_age_ms:
                        await delivery.ack()
                        return
                except Exception as exc:
                    self._logger.warning(
                        "[%s] Failed to parse NATS broadcast timestamp: %s",
                        self.id,
                        exc,
                    )

        should_process = is_for_me or is_for_my_type or is_for_team_lead
        if is_for_all and isinstance(
            msg_content,
            (
                AgentStatus,
                ControlInstruction,
                ManagerRuntimeWorkCommand,
                TaskProgressStatus,
                TaskSteering,
            ),
        ):
            should_process = True
        if not should_process:
            self._logger.warning(
                "NATS delivery does not target this agent. "
                "event=agent_nats_delivery_target_rejected agent_id=%s "
                "team_id=%s message_id=%s task_id=%s routing_key=%s "
                "logical_route=%s subject=%s",
                self.id,
                self.team_id,
                msg_header.message_id,
                getattr(msg_content, "task_id", None),
                effective_to_routing_path,
                delivery.logical_route,
                delivery.transport_subject,
            )
            await delivery.ack()
            return

        if await divert_request_during_wind_down(self, delivery):
            return

        requires_execution_slot = self._requires_execution_slot(msg_envelope)
        keepalive_task: Optional[asyncio.Task] = None
        try:
            if await handle_busy_delivery(self, delivery, requires_execution_slot):
                return
            if requires_execution_slot:
                keepalive_task = asyncio.create_task(
                    self._ensure_nats_transport_adapter().keepalive_while(
                        delivery, asyncio.current_task()
                    )
                )
            scope = (
                self._execution_scope()
                if requires_execution_slot
                else contextlib.nullcontext()
            )
            async with scope:
                await self._dispatch_message(
                    msg_header=msg_header,
                    msg_content=msg_content,
                    msg_meta=msg_meta,
                    message_handled_at=datetime.now(timezone.utc).isoformat(),
                    source_queue_name=delivery.source_queue_name or delivery.logical_route,
                )
            await delivery.ack()
        except Exception as exc:
            self._logger.exception(
                "[%s] Exception processing NATS delivery: %s", self.id, exc
            )
            if (
                delivery.redelivery_count is not None
                and delivery.redelivery_count
                >= max(0, (delivery.max_deliver or self.nats_max_deliver) - 1)
            ):
                # The transport owns final failure settlement: it preserves
                # this delivery before ending it. Returning here would bypass
                # PullRunner's failed-handler path and lose the message.
                raise
            else:
                await delivery.nak()
        finally:
            if keepalive_task is not None:
                keepalive_task.cancel()
                # Consume child cancellation without swallowing cancellation of
                # this delivery owner during transport shutdown.
                await asyncio.gather(keepalive_task, return_exceptions=True)
            await self._refresh_self_agent_state(is_exiting=self._should_exit.is_set())
            schedule_pending_work(self)

    async def on_nats_operational_delivery(self, delivery: InboundDelivery) -> None:
        """Process live NATS operational fanout messages."""

        msg_envelope = delivery.envelope
        msg_header = msg_envelope.msg_header
        msg_content = msg_envelope.msg_content
        msg_meta = msg_envelope.msg_meta or MessageMeta()
        if msg_header.from_agent_id == self.id:
            await delivery.ack()
            return
        if self._message_targets_other_team(msg_envelope):
            await delivery.ack()
            return
        if not self._is_operational_content(msg_content):
            self._logger.warning(
                "[%s] Ignoring non-operational NATS message on operational subject: %s.",
                self.id,
                type(msg_content).__name__,
            )
            await delivery.ack()
            return

        if (
            isinstance(msg_content, (TaskStatus, AgentStatus))
            and msg_content.status_type == MessageMetaType.RESPONSE
        ):
            await self._dispatch_message(
                msg_header=msg_header, msg_content=msg_content, msg_meta=msg_meta
            )
            await delivery.ack()
            return

        if not self._operational_target_matches(
            msg_content=msg_content,
            msg_header=msg_header,
            msg_meta=msg_meta,
        ):
            await delivery.ack()
            return

        await self._dispatch_message(
            msg_header=msg_header, msg_content=msg_content, msg_meta=msg_meta
        )
        await delivery.ack()

    def get_self_status(
        self,
        info_type: ControlType = ControlType.STATUS_REQUEST_BASIC,
        is_exiting=False,
    ) -> AgentStatus:
        """
        Generates an AgentStatus message containing the agent's current status.

        Args:
            info_type (ControlType, optional): The desired level of information.
            is_exiting (bool, optional): If True, indicates that the agent is exiting.

        Returns:
            AgentStatus: A message containing the agent's status information.
        """
        status_metrics = self._build_status_metrics()
        diagnostic_dict = {
            "user_routing_key": self.user_input_queue_name,
            "self_routing_key": self.id,
            "memory_graph_enabled": self.memory_graph_enabled,
            "memory_graph_available": self._memory_graph_available,
            "llm_model": self.model_id,
            "model_provider": self.model_provider,
            "reasoning_effort": self.reasoning_effort,
            "reasoning_summary": self.reasoning_summary,
            "litellm_api_mode": self.litellm_api_mode,
            "context_window_tokens": self.context_window_tokens,
            "supports_image_input": self.supports_image_input,
            "supports_audio_input": self.supports_audio_input,
            "supports_file_input": self.supports_file_input,
            "authorization_enabled": authorization_enabled(self.authorization_config),
            **status_metrics,
        }
        if self._memory_graph_degraded_reason:
            diagnostic_dict["memory_graph_degraded_reason"] = (
                self._memory_graph_degraded_reason
            )
        if self._runtime_repair_anomaly_counts:
            diagnostic_dict["runtime_repair_anomaly_counts"] = dict(
                sorted(self._runtime_repair_anomaly_counts.items())
            )
        if self._skill_catalog_runtime_state.last_error:
            diagnostic_dict["skill_catalog_refresh_error"] = (
                self._skill_catalog_runtime_state.last_error
            )

        pressure_snapshot: Dict[str, Any] = {}
        try:
            from custom_tools.providers.diagnostics import (
                build_runtime_pressure_snapshot,
            )

            if self.task_manager.current_task_id:
                pressure_snapshot = build_runtime_pressure_snapshot(self)
                diagnostic_dict["runtime_pressure"] = pressure_snapshot
        except Exception as exc:
            diagnostic_dict["runtime_pressure_error"] = str(exc)

        # Get tool names - handle both function-based tools and class-based tools
        tool_names = []
        for t in self.tools:
            if hasattr(t, "name"):
                tool_names.append(t.name)
            elif hasattr(t, "__name__"):
                tool_names.append(t.__name__)
            else:
                tool_names.append(str(t))

        available_skill_names: set[str] = set()
        if not is_exiting and self._strands_agent is not None:
            for plugin in self.plugins or []:
                available_skill_names.update(
                    list_agent_skill_names(plugin, self._strands_agent)
                )

        agent_status = AgentStatus(
            self_id=self.id,
            team_id=self.team_id,
            name=self.name,
            agent_role=self.role,
            named_agent_type=self.named_agent_type,
            is_paused=self.paused,
            is_executing=self.is_executing,
            has_execution_capacity=status_metrics["has_execution_capacity"],
            is_busy=self.is_executing,
            is_exiting=is_exiting,
            accepting_work=not is_winding_down(self),
            is_team_lead=self.is_team_lead,
            top_level_queue_depth=status_metrics["top_level_queue_depth"],
            waiting_top_level_task_count=status_metrics["waiting_top_level_task_count"],
            deferred_message_count=status_metrics["deferred_message_count"],
            current_task_id=pressure_snapshot.get("current_task_id"),
            current_task_age_seconds=pressure_snapshot.get("task_age_seconds"),
            context_window_tokens=pressure_snapshot.get("context_window_tokens"),
            estimated_context_tokens=pressure_snapshot.get("estimated_context_tokens"),
            context_fill_ratio=pressure_snapshot.get("context_fill_ratio"),
            context_pressure_level=pressure_snapshot.get("context_pressure_level"),
            tool_call_budget_fill_ratio=pressure_snapshot.get(
                "tool_call_budget_fill_ratio"
            ),
            tool_result_budget_fill_ratio=pressure_snapshot.get(
                "tool_result_budget_fill_ratio"
            ),
            last_seen_at=datetime.now(timezone.utc).isoformat(),
            process_identity=own_process_identity(self),
            status_type=MessageMetaType.RESPONSE,
            info_type=info_type,
            tools=[] if is_exiting else tool_names,
            available_skills=sorted(available_skill_names, key=str.casefold),
            status_findings=[] if is_exiting else agent_status_findings(self),
        )

        if info_type == ControlType.STATUS_REQUEST_FULL:
            diagnostic_dict["current_task_id"] = self.task_manager.current_task_id
            if self.task_manager.current_task_id:
                diagnostic_dict["current_task_status"] = self.task_manager.get_task(
                    task_id=diagnostic_dict["current_task_id"]
                ).task_state

            diagnostic_dict["model_temperature"] = self.temperature
            diagnostic_dict["uptime"] = self._format_uptime()
            if self._schedule_store is not None:
                try:
                    diagnostic_dict["schedule_telemetry"] = (
                        self._schedule_store.build_telemetry_snapshot(
                            include_schedules=True,
                        )
                    )
                except Exception as exc:
                    diagnostic_dict["schedule_telemetry_error"] = str(exc)
            if self._runtime_repair_anomaly_counts:
                diagnostic_dict["runtime_repair_anomalies"] = (
                    self._runtime_repair_anomaly_snapshot()
                )

        agent_status.diagnostic = diagnostic_dict
        return agent_status

    async def _handle_task_query_message(self, *args, **kwargs):
        return await task_message_runtime._handle_task_query_message(
            self, *args, **kwargs
        )

    async def _handle_secure_credential_collection_result(self, *args, **kwargs):
        return await task_message_runtime._handle_secure_credential_collection_result(
            self, *args, **kwargs
        )

    async def _handle_tool_setup_result(self, *args, **kwargs):
        return await tool_setup_runtime.handle_tool_setup_result(self, *args, **kwargs)

    async def _handle_task_request(self, *args, **kwargs):
        return await task_message_runtime._handle_task_request(self, *args, **kwargs)

    async def _handle_delivery_receipt(self, *args, **kwargs):
        return await task_message_runtime._handle_delivery_receipt(
            self, *args, **kwargs
        )

    async def _check_and_process_next_task(self, *args, **kwargs):
        if not self._can_continue_pending_work():
            return False
        refresh_state = True
        try:
            # Admission must be exclusive before its first storage await. An
            # empty probe must not publish busy/idle transitions: receiving
            # those presence messages would trigger another empty probe.
            async with self._execution_gate.hold():
                started = await task_message_runtime._check_and_process_next_task(
                    self, *args, **kwargs
                )
                refresh_state = started
                return started
        finally:
            if refresh_state:
                await self._refresh_self_agent_state()

    async def _apply_task_processing_result(self, *args, **kwargs):
        return await task_message_runtime._apply_task_processing_result(
            self, *args, **kwargs
        )

    async def _resume_interrupted_task_turn(self, task_id: str):
        if self.is_executing and not self._execution_gate.owned_by_current_task:
            return None
        async with self._execution_scope():
            return await resume_interrupted_task_turn(self, task_id)

    async def _handle_task_response(self, *args, **kwargs):
        return await task_message_runtime._handle_task_response(self, *args, **kwargs)

    async def handle_info_req_message(
        self,
        info_request: AgentStatus,
        msg_header: MessageHeader,
        msg_meta: MessageMeta,
    ):
        """
        Handles incoming AgentStatus messages by determining whether it is a request or a reply.

        Args:
            info_request (AgentStatus): The parsed AgentStatus payload.
            msg_header (MessageHeader): The MessageHeader of the incoming message.
            msg_meta (MessageMeta): The MessageMeta of the incoming message.
        """
        if info_request.status_type == MessageMetaType.REQUEST:
            await repair_agent_skill_catalog(
                self,
                reason="agent_status_request",
            )
            response_key = await resolve_requester_routing_key(
                incoming_header=msg_header,
                incoming_meta=msg_meta,
                agent_id_by_name=self._agent_id_by_name,
                named_agent_type_queue_map=self.named_agent_type_queue_map,
                team_id=self.team_id,
                logger=self._logger,
                agent_id=self.id,
                repair_recorder=self._record_runtime_repair_anomaly,
            )
            message_env = MessageEnvelope(
                msg_content=self.get_self_status(info_type=info_request.info_type),
                msg_header=MessageHeader(
                    from_agent_id=self.id,
                    to_routing_path=response_key,
                ),
                msg_meta=MessageMeta(
                    msg_correlation_id=msg_meta.msg_correlation_id,
                    conversation_id=getattr(msg_meta, "conversation_id", None),
                ),
            )
            if response_key:
                self._logger.debug(
                    "[%s] event=agent_status_response_send correlation_id=%s requester_from_agent_id=%s "
                    "response_key=%s local_agent_id=%s local_team_id=%s local_named_agent_type=%s "
                    "info_type=%s",
                    self.id,
                    getattr(msg_meta, "msg_correlation_id", None),
                    getattr(msg_header, "from_agent_id", None),
                    response_key,
                    self.id,
                    self.team_id,
                    self.named_agent_type,
                    info_request.info_type,
                )
                await self._send_operational_message(message_env)
            else:
                self._logger.error(
                    "[%s] Received AgentStatus request with no response routing key. "
                    "event=agent_status_response_missing_route correlation_id=%s "
                    "incoming_from_agent_id=%s incoming_response_routing_key=%s "
                    "incoming_from_agent_name=%s incoming_to_agent_name=%s "
                    "incoming_to_named_agent_type=%s",
                    self.id,
                    getattr(msg_meta, "msg_correlation_id", None),
                    getattr(msg_header, "from_agent_id", None),
                    getattr(msg_header, "response_routing_key", None),
                    getattr(msg_meta, "from_agent_name", None),
                    getattr(msg_meta, "to_agent_name", None),
                    getattr(msg_meta, "to_named_agent_type", None),
                )
        else:
            await self.handle_agent_status_message(
                agent_status=info_request, msg_header=msg_header, msg_meta=msg_meta
            )

    async def handle_agent_status_message(
        self,
        agent_status: AgentStatus,
        msg_header: MessageHeader,
        msg_meta: MessageMeta,
    ):
        """
        Processes a status message sent by another agent.

        Args:
            agent_status (AgentStatus): The received AgentStatus payload.
            msg_header (MessageHeader): The MessageHeader of the incoming message.
            msg_meta (MessageMeta): The MessageMeta of the incoming message.
        """
        agent_id = _validated_agent_status_id(
            agent_status,
            logger=self._logger,
            receiving_agent_id=self.id,
            msg_header=msg_header,
            msg_meta=msg_meta,
        )
        if not agent_id:
            return
        agent_name = agent_status.name or f"Agent_{agent_id}"

        if agent_id == self.id:
            self._logger.debug(
                f"[{self.id}] Received status message about self. Ignoring."
            )
            return

        self._logger.debug(
            f"[{self.id}] Received status update for agent '{agent_name}' ({agent_id})."
        )

        # The sender's own time, never later than now: presence is replayed
        # when the lead subscribes, so the arrival time made agents that had
        # just crashed look alive (agent_liveness.received_status_time).
        agent_status.last_seen_at = received_status_time(self, agent_status)
        self.known_agents.agent_map[agent_id] = agent_status

        self._resolve_pending_agent_status_probes(
            agent_status,
            getattr(msg_meta, "msg_correlation_id", None),
        )

        # Update name lookup if a name is provided
        if agent_status.name:
            self._agent_id_by_name[agent_status.name] = agent_id

        # Only team leads maintain records of other agents, and only with a
        # newer time: a delayed older status must not overwrite a row the agent
        # wrote itself (for example its exiting mark).
        if self._agent_state_store and self.is_team_lead:
            await self.task_manager.operations_runtime.run(
                self._agent_state_store.upsert_if_newer, agent_status
            )

        if agent_status.is_exiting:
            self._logger.warning(
                f"[{self.id}] Agent '{agent_name}' ({agent_id}) is exiting."
            )
            if agent_id in self.known_agents.agent_map:
                del self.known_agents.agent_map[agent_id]
            if agent_status.name and agent_status.name in self._agent_id_by_name:
                del self._agent_id_by_name[agent_status.name]
            if self.is_team_lead and agent_status.name:
                pending_direct_subtasks = [
                    subtask_info
                    for subtask_info in await self.task_manager.operations_runtime.run(
                        self.task_manager.get_pending_subtasks,
                    )
                    if getattr(subtask_info.meta, "to_agent_name", None)
                    == agent_status.name
                ]
                for subtask_info in pending_direct_subtasks:
                    parent_task_id = await self.task_manager.operations_runtime.run(
                        self.task_manager.get_parent_task_id, subtask_info.task.task_id
                    )
                    if not parent_task_id:
                        continue
                    await self._fail_subtask_and_reconcile_parent(
                        parent_task_id=parent_task_id,
                        subtask_info=subtask_info,
                        error_message=(
                            f"Delegated subtask failed because assigned agent "
                            f"{agent_status.name} exited before replying."
                        ),
                    )

            # Keep the agent in the persistent store to maintain history,
            # but log that it has exited.
            if self._agent_state_store and self.is_team_lead:
                self._logger.info(
                    f"[{self.id}] Team lead maintained exited agent '{agent_name}' ({agent_id}) in AgentStateStore."
                )
        else:
            self._logger.debug(
                f"[{self.id}] Agent '{agent_name}' Status: Type={agent_status.named_agent_type}, "
                f"Executing={agent_status.is_executing}, HasCapacity={agent_status.has_execution_capacity}"
            )

        # AgentStatus traffic is operational metadata; keep it out of the LLM loop.
        return

    def _should_apply_stale_broadcast_filter(
        self, msg_envelope: MessageEnvelope
    ) -> bool:
        content = getattr(msg_envelope, "msg_content", None)
        if (
            isinstance(content, AgentStatus)
            and content.status_type == MessageMetaType.REQUEST
        ):
            return False
        return True

    async def handle_control_message(
        self,
        control_instruction: ControlInstruction,
        *,
        msg_header: MessageHeader,
        msg_meta: MessageMeta,
    ):
        """
        Handles messages with a ControlInstruction payload.

        Args:
            control_instruction (ControlInstruction): The extracted ControlInstruction object.
        """
        try:
            control_type = control_instruction.type
            instruct_string = control_instruction.instruction.instruction

            if control_type == ControlType.CONTROL_MSG:
                if instruct_string == "exit":
                    self._logger.info(
                        f"[{self.id}] Agent received 'exit' control instruction."
                    )
                    await self.exit_agent()
                elif instruct_string == "pause":
                    self._logger.info(
                        f"[{self.id}] Agent received 'pause' control instruction."
                    )
                    await self.pause()
                elif instruct_string == "resume":
                    self._logger.info(
                        f"[{self.id}] Agent received 'resume' control instruction."
                    )
                    await self.resume()
                elif instruct_string == "debug_context_dump":
                    settings = dict(control_instruction.runtime_settings or {})
                    applied = self.apply_runtime_context_dump_settings(
                        enabled=bool(settings.get("enabled", False)),
                        mode=str(settings.get("mode") or "on_error"),
                        source="web_chat_control",
                    )
                    await self._send_control_status_response(
                        msg_header=msg_header,
                        msg_meta=msg_meta,
                        status_text=(
                            "Context debug dump settings applied: "
                            f"enabled={str(applied['enabled']).lower()} mode={applied['mode']}."
                        ),
                    )
                elif instruct_string == "stop":
                    self._logger.info(
                        f"[{self.id}] Agent received 'stop' control instruction: "
                        f"target_task_id={control_instruction.target_task_id or 'unknown'} "
                        f"target_correlation_id={control_instruction.target_correlation_id or 'unknown'} "
                        f"conversation_id={getattr(msg_meta, 'conversation_id', None) or 'unknown'} "
                        f"from_agent_id={getattr(msg_header, 'from_agent_id', None) or 'unknown'} "
                        f"from_agent_name={getattr(msg_meta, 'from_agent_name', None) or 'unknown'} "
                        f"cascade_to_subtasks={bool(control_instruction.cascade_to_subtasks)}"
                    )
                    task_info = await self._resolve_control_target_task_info(
                        control_instruction=control_instruction,
                        msg_meta=msg_meta,
                    )
                    if task_info is None:
                        self._logger.info(
                            f"[{self.id}] Stop control did not match an active task: "
                            f"target_task_id={control_instruction.target_task_id or 'unknown'} "
                            f"target_correlation_id={control_instruction.target_correlation_id or 'unknown'} "
                            f"current_task_id={self.task_manager.current_task_id or 'none'}"
                        )
                        await self._send_control_status_response(
                            msg_header=msg_header,
                            msg_meta=msg_meta,
                            status_text="No active task matched the stop request.",
                        )
                        return
                    self._logger.info(
                        f"[{self.id}] Stop control resolved to task {task_info.task.task_id} "
                        f"in state {task_info.state.value}."
                    )

                    stop_reason = (
                        control_instruction.reason or "Task cancelled by stop command."
                    )
                    root_task_id = (
                        await self.task_manager.operations_runtime.run(
                            self.task_manager.resolve_root_task_id,
                            task_info.task.task_id,
                        )
                        or task_info.task.task_id
                    )
                    active_lease = await self.task_manager.operations_runtime.run(
                        self.task_manager.get_active_runtime_lease, root_task_id
                    )
                    if active_lease is not None:
                        event = await self.task_manager.operations_runtime.run(
                            self._runtime_coordinator().record_envelope_event,
                            root_task_id=root_task_id,
                            event_type="control_stop",
                            event_class="preemptive",
                            content=control_instruction,
                            header=msg_header,
                            meta=msg_meta,
                            status="received",
                            related_task_id=task_info.task.task_id,
                            blocked_by_lease_id=active_lease.lease_id,
                        )
                        await self.task_manager.operations_runtime.run(
                            self._runtime_coordinator().request_interrupt,
                            root_task_id=root_task_id,
                            event_id=event.event_id,
                            reason=stop_reason,
                        )
                    cascade_to_subtasks = (
                        bool(control_instruction.cascade_to_subtasks)
                        or task_info.state == ProgressState.WAITING
                    )
                    result = await self._stop_task_execution(
                        task_info=task_info,
                        reason=stop_reason,
                        cascade_to_subtasks=cascade_to_subtasks,
                    )
                    await self._send_control_status_response(
                        msg_header=msg_header,
                        msg_meta=msg_meta,
                        status_text=(
                            f"Stopped task {result['task_id']} "
                            f"(cancelled={result['cancelled_task_count']}, "
                            f"pending_subtasks={result['pending_subtask_count']}, "
                            f"subtask_stop_signals={result['stop_signals_sent']})."
                        ),
                    )
                else:
                    self._logger.warning(
                        f"[{self.id}] Received unknown CONTROL_MSG instruction: '{instruct_string}'"
                    )
        except Exception as e:
            self._logger.error(
                f"[{self.id}] Agent failed to process control instruction payload: {e}"
            )

    async def _suppress_post_activation_plan_bypass(
        self,
        *,
        incoming_content: ContentType,
        outgoing_content: ContentType,
    ) -> Optional[Task]:
        """
        Prevent a root task from doing extra work in the same turn after it has
        activated runtime-owned plan execution.

        Once plan steps are active, the root turn should end with a suppressed
        placeholder response. Delegated task requests or TaskQuery requests from
        that same turn would bypass the plan outer loop and reintroduce the
        oversized inner-loop behavior the plan primitive is meant to avoid.
        """
        if not isinstance(incoming_content, Task):
            return None
        if incoming_content.parent_task_id is not None:
            return None
        if await self.task_manager.operations_runtime.run(
            self.task_manager.is_plan_step_task, incoming_content.task_id
        ):
            return None
        if await self.task_manager.operations_runtime.run(
            self.task_manager.is_isolated_recovery_task, incoming_content.task_id
        ):
            return None
        if not await self.task_manager.operations_runtime.run(
            self.task_manager.is_plan_execution_root_active, incoming_content.task_id
        ):
            return None

        bypass_kind: Optional[str] = None
        if (
            isinstance(outgoing_content, Task)
            and outgoing_content.task_type == MessageMetaType.REQUEST
        ):
            bypass_kind = "delegated_task_request"
        elif (
            isinstance(outgoing_content, TaskQuery)
            and outgoing_content.query_type == MessageMetaType.REQUEST
        ):
            bypass_kind = "task_query_request"
        if bypass_kind is None:
            return None

        now_ts = datetime.now(timezone.utc).isoformat()
        target = None
        if isinstance(outgoing_content, Task):
            target = getattr(
                outgoing_content, "target_named_agent_type", None
            ) or getattr(outgoing_content, "target_agent_name", None)
        await self.task_manager.operations_runtime.run(
            self.task_manager.append_history_entry,
            incoming_content.task_id,
            (
                "Runtime plan execution is active; suppressed an immediate "
                f"{bypass_kind} emitted after plan activation so work continues through plan steps."
            ),
            entry_type=TaskHistoryType.PLAN_EVENT,
            importance=TaskHistoryImportance.HIGH,
            timestamp=now_ts,
            agent_name=self.name,
            agent_id=self.id,
            tracks_to_plan=True,
            details={
                "action": "suppress_post_activation_plan_bypass",
                "bypass_kind": bypass_kind,
                "target": target,
            },
        )
        await self.task_manager.operations_runtime.run(
            self.task_manager.record_plan_action,
            incoming_content.task_id,
            PlanActionType.RECORD_FAILURE,
            f"Suppressed post-activation output that would bypass runtime plan steps: {bypass_kind}.",
            status=PlanActionStatus.RECORDED,
            reason="post_activation_plan_bypass",
            user_visible=False,
            details={
                "bypass_kind": bypass_kind,
                "target": target,
            },
            agent_name=self.name,
            agent_id=self.id,
            timestamp=now_ts,
        )
        await self.task_manager.operations_runtime.run(
            self.task_manager.update_main_task_state,
            incoming_content.task_id,
            ProgressState.WAITING,
            now_ts,
        )
        return Task(
            task_id=incoming_content.task_id,
            task_type=MessageMetaType.REQUEST,
            problem_statement=(
                "Runtime plan execution is active; suppressed an immediate output that would bypass "
                "internal plan-step execution."
            ),
        )

    def _comms_message_from_turn_action(self, *args, **kwargs):
        return turn_action_outbox_runtime._comms_message_from_turn_action(
            self, *args, **kwargs
        )

    def _turn_decision_from_structured_output(self, *args, **kwargs):
        return turn_action_outbox_runtime._turn_decision_from_structured_output(
            self, *args, **kwargs
        )

    def _turn_decision_from_comms_message(self, *args, **kwargs):
        return turn_action_outbox_runtime._turn_decision_from_comms_message(
            self, *args, **kwargs
        )

    def _turn_action_retry_delay_seconds(self, *args, **kwargs):
        return turn_action_outbox_runtime._turn_action_retry_delay_seconds(
            self, *args, **kwargs
        )

    async def _schedule_turn_action_recovery(self, *args, **kwargs):
        return await turn_action_outbox_runtime._schedule_turn_action_recovery(
            self, *args, **kwargs
        )

    async def _execute_turn_action_record(self, *args, **kwargs):
        return await turn_action_outbox_runtime._execute_turn_action_record(
            self, *args, **kwargs
        )

    async def _execute_turn_action_with_recovery(self, *args, **kwargs):
        return await turn_action_outbox_runtime._execute_turn_action_with_recovery(
            self, *args, **kwargs
        )

    async def _notify_user_delegated_work_started(self, *args, **kwargs):
        return await turn_action_outbox_runtime._notify_user_delegated_work_started(
            self, *args, **kwargs
        )

    async def _execute_turn_decision_outbox(self, *args, **kwargs):
        return await turn_action_outbox_runtime._execute_turn_decision_outbox(
            self, *args, **kwargs
        )

    @property
    def turn_executor(self) -> llm_turn_runtime.LLMTurnExecutor:
        if self._llm_turn_executor is None:
            self._llm_turn_executor = llm_turn_runtime.LLMTurnExecutor(
                settings=self._turn_runtime_settings,
                model=lambda: self._strands_agent,
                context=lambda: self._context_engine,
                debug_dump=lambda: self._context_debug_dump_plugin,
                logger=self._logger,
                tasks=TurnTaskOperations(
                    runtime=self.task_manager.operations_runtime,
                    append_history_entry=self.task_manager.append_history_entry,
                    read_partial_tool_call_limit=self.task_manager.read_partial_tool_call_limit,
                    read_runtime_resume_directive=self.task_manager.read_runtime_resume_directive,
                    release_turn_start_notes=self.task_manager.release_turn_start_notes,
                    count_recoverable_pauses=self.task_manager.count_recoverable_pauses,
                    get_task_info=self.task_manager.get_task_info,
                    is_isolated_recovery_task=self.task_manager.is_isolated_recovery_task,
                    is_plan_step_task=self.task_manager.is_plan_step_task,
                    summarize_task_history_markdown=self.task_manager.summarize_task_history_markdown,
                    objective_work_coordinator=self.task_manager.objective_work_coordinator,
                ),
                effects=TurnRuntimeEffects(
                    clear_active_execution_context=self._clear_active_execution_context,
                    create_context_engine=self._create_context_engine,
                    execute_turn_decision_outbox=self._execute_turn_decision_outbox,
                    prepare_objective_work_completion=lambda task, meta, session: prepare_objective_work_completion(self, task, meta, session),
                    friendly_budget_reason=self._friendly_budget_reason,
                    history_task_id_for_content=self._history_task_id_for_content,
                    max_user_guided_pauses_per_task=self._max_user_guided_pauses_per_task,
                    raise_if_stop_requested=self._raise_if_stop_requested,
                    record_model_invocation_success=self._record_model_invocation_success,
                    record_tool_call_limit_exceeded=self._record_tool_call_limit_exceeded,
                    record_tool_result_budget_events=self._record_tool_result_budget_events,
                    refresh_skills_plugins=self._refresh_skills_plugins,
                    reset_llm_conversation=self._reset_llm_conversation,
                    resolved_max_tool_calls_per_task=self._resolved_max_tool_calls_per_task,
                    runtime_budget_policy_mode=self._runtime_budget_policy_mode,
                    runtime_budget_trigger_enabled=self._runtime_budget_trigger_enabled,
                    send_paused_subtask_response=self._send_paused_subtask_response,
                    handle_delegated_provider_limit=lambda **kwargs: handle_delegated_provider_limit(
                        self, **kwargs
                    ),
                    send_runtime_pause_query=self._send_runtime_pause_query,
                    set_active_execution_context=self._set_active_execution_context,
                    select_turn_model=lambda task_id: select_turn_model(self, task_id),
                    turn_decision_from_comms_message=self._turn_decision_from_comms_message,
                    turn_decision_from_structured_output=self._turn_decision_from_structured_output,
                    emit_activity_event=self.emit_activity_event,
                    refresh_persistent_llm_override=self.refresh_persistent_llm_override,
                    refresh_runtime_llm_selection=self.refresh_runtime_llm_selection,
                    should_notify_user=lambda: (
                        model_failover_runtime.should_notify_user(self)
                    ),
                    handle_provider_failure=lambda **kwargs: (
                        model_failover_runtime.handle_provider_failure(self, **kwargs)
                    ),
                    requeue_task_after_failover=lambda **kwargs: (
                        model_failover_runtime.requeue_task_after_failover(
                            self, **kwargs
                        )
                    ),
                    ensure_fresh_strands_agent_for_new_turn=lambda **kwargs: (
                        task_steering_runtime.ensure_fresh_strands_agent_for_new_turn(
                            self, **kwargs
                        )
                    ),
                    consume_pending_proactive_guidance_message=lambda task_id: (
                        task_steering_runtime.consume_pending_proactive_guidance_message(
                            self, task_id
                        )
                    ),
                    send_interrupt_request_for_result=lambda **kwargs: (
                        task_steering_runtime.send_interrupt_request_for_result(
                            self, **kwargs
                        )
                    ),
                    transform_result=self._transform_turn_result,
                    publish=lambda **kwargs: self.team_comms.send_message(**kwargs),
                ),
            )
        return self._llm_turn_executor

    def _turn_runtime_settings(self) -> TurnRuntimeSettings:
        # A person's own model shows in the turn's settings while it is in use.
        return turn_settings_with_person_model(self, TurnRuntimeSettings(
            context_engine=self.context_engine,
            debug=self.debug,
            id=self.id,
            inner_loop_context=self.inner_loop_context,
            instance_config_path=self.instance_config_path,
            litellm_api_base=self.litellm_api_base,
            litellm_api_mode=self.litellm_api_mode,
            llm_override_path=self.llm_override_path,
            model_id=self.model_id,
            model_provider=self.model_provider,
            name=self.name,
            named_agent_type=self.named_agent_type,
            otel_enable_traces=self.otel_enable_traces,
            otel_enabled=self.otel_enabled,
            reasoning_effort=self.reasoning_effort,
            reasoning_summary=self.reasoning_summary,
            stream=self.stream,
            supports_image_input=self.supports_image_input,
            system_prompt=self.system_prompt,
            team_id=self.team_id,
            workspace_dir=self.workspace_dir,
            instance_root=self.instance_root,
            is_team_lead=bool(self.is_team_lead),
        ))

    def _transform_turn_result(
        self, content: ContentType, result: AgentResult
    ) -> AgentResult:
        if self._agent_result_transformer is None:
            return result
        transformed = self._agent_result_transformer(self, content, result)
        return transformed if transformed is not None else result

    async def process_llm_interaction(
        self, msg_content: ContentType, msg_header: MessageHeader, msg_meta: MessageMeta
    ) -> ContentType:
        async with self._execution_scope():
            # Stop cancels this child turn, never the transport/runtime owner.
            # Keep the execution gate held until the SDK finishes unwinding.
            return await asyncio.create_task(
                self.turn_executor.process_llm_interaction(msg_content, msg_header, msg_meta)
            )

    async def exit_agent(self):
        """Performs cleanup and signals the agent to stop."""
        self._logger.info(f"[{self.id}] Stopping agent as per control message.")

        # Cancel the scheduler background task
        if self._scheduler_task and not self._scheduler_task.done():
            self._scheduler_task.cancel()
            self._logger.info(f"[{self.id}] Scheduler task cancelled.")

        self.set_exit_flag()

    async def pause(self):
        """Pauses the agent, preventing it from processing new messages."""
        self._logger.info(f"[{self.id}] Pausing agent as per control message.")
        self.paused = True
        await self._refresh_self_agent_state()

    async def resume(self):
        """Resumes the agent, allowing it to process messages again."""
        self._logger.info(f"[{self.id}] Resuming agent as per control message.")
        self.paused = False
        await self._refresh_self_agent_state()
        schedule_pending_work(self)

    # ──────────────────────────────────────────────────────────────────────
    # SCHEDULED MESSAGE LOGIC — see ../ua-architecture/docs/scheduling_and_automation_architecture.md.
    # If you change any of the methods below,
    # you MUST update the documentation to stay in sync.
    # ──────────────────────────────────────────────────────────────────────

    async def _handle_scheduled_message(
        self,
        msg: ScheduledMessage,
        *,
        msg_header: Optional[MessageHeader] = None,
        msg_meta: Optional[MessageMeta] = None,
    ) -> None:
        await handle_scheduled_message(
            self,
            msg,
            msg_header=msg_header,
            msg_meta=msg_meta,
        )

    def _is_heartbeat_launcher(self, scheduled_message: ScheduledMessage) -> bool:
        return is_heartbeat_launcher(scheduled_message)

    async def _enqueue_heartbeat_items(
        self, scheduled_message: ScheduledMessage
    ) -> bool:
        return await enqueue_heartbeat_items(self, scheduled_message)

    async def _enqueue_fired_scheduled_task(
        self,
        schedule_id: str,
        scheduled_message: ScheduledMessage,
        *,
        from_runtime_mailbox: bool = False,
        schedule_admission=None,
        claim_token: Optional[str] = None,
    ) -> bool:
        return await enqueue_fired_scheduled_task(
            self,
            schedule_id,
            scheduled_message,
            from_runtime_mailbox=from_runtime_mailbox,
            schedule_admission=schedule_admission,
            claim_token=claim_token,
        )

    async def _scheduler_loop(self) -> None:
        await scheduler_loop(self)
