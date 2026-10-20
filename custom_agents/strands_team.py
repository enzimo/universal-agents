"""
Agent configuration and instantiation using Strands Agents SDK.

This module defines the supported agent types and creates configured
`UniversalAgent` instances for the runtime.
"""

import asyncio
import logging
import json
import os
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict

from default_values import (
    ACTIVITY_COLLECTOR_DEFAULTS,
    AGENT_DEFAULTS,
    CONVERSATION_COMPACTION_DEFAULTS,
    EMBEDDING_DEFAULTS,
    LLM_DEFAULTS,
    OTEL_DEFAULTS,
    STORAGE_DEFAULTS,
    TOOL_POLICY_DEFAULTS,
    TRANSPORT_DEFAULTS,
)
from runtime_settings.instance_layout import InstanceLayout
from custom_agents.lease_loss_wind_down import is_winding_down
from custom_agents.agent_instantiation_skill import (
    ensure_runtime_agent_instantiation_skill,
)
from custom_agents.agent_team_runtime import LocalAgentTeamRuntime
from custom_messages.contracts.schedule_worker_v1 import AgentLaunchResultV1, AgentLaunchState, AgentLaunchV1
from custom_agents.agent_instructions import strip_access_prompt
from custom_agents.agent_catalog import load_agent_type_map
from custom_agents.access_runtime import authorization_enabled
from custom_messages.contracts.runtime_v1 import TEAM_ARCHITECT_AGENT_TYPE
from storage.backends import (
    STORAGE_BACKEND_POSTGRES,
    STORAGE_BACKEND_SQLITE,
)
from runtime_settings.code_sandbox_contract import (
    ENV_CODE_SANDBOX_EXTRA_READ_PATHS,
    ENV_CODE_SANDBOX_EXTRA_WRITE_PATHS,
    ENV_CODE_SANDBOX_MODE,
)
from custom_tools.provider_contract import tool_name
from custom_agents.team_config_helpers import (
    HIDDEN_WITHOUT_AUTHORIZATION,
    build_system_prompt,
    create_agent_description as create_agent_description,
    fill_backstory_template,
    filter_agent_tools,
    get_all_named_agents_summary as get_all_named_agents_summary,
)
from custom_agents.sdk.strands.mcp import (
    merge_external_mcp_server_configs,
    resolve_mcp_profile_configs,
)
from custom_agents.sdk.strands.skills import build_skills_plugin
from custom_agents.workspace_prompt_context import build_workspace_prompt_context
from custom_tools.runtime_context import set_agent_ref
from custom_tools.agent_team_port import configure_agent_type_source
from storage.config import resolve_storage_settings

SECURE_CREDENTIAL_WORKFLOWS_SKILL_DIR = (
    Path(__file__).resolve().parent / "skill_templates" / "secure-credential-workflows"
)
RUNTIME_OPERATIONS_WORKFLOWS_SKILL_DIR = (
    Path(__file__).resolve().parent / "skill_templates" / "runtime-operations-workflows"
)
SHELL_EXECUTION_WORKFLOWS_SKILL_DIR = (
    Path(__file__).resolve().parent / "skill_templates" / "shell-execution-workflows"
)
CONVERT_DOCUMENTS_TO_MARKDOWN_SKILL_DIR = (
    Path(__file__).resolve().parent
    / "skill_templates"
    / "convert-documents-to-markdown"
)
LONG_HORIZON_PLAN_EXECUTION_SKILL_DIR = (
    Path(__file__).resolve().parent / "skill_templates" / "long-horizon-plan-execution"
)
XURL_SKILL_DIR = Path(__file__).resolve().parent / "skill_templates" / "xurl"
HF_CLI_SKILL_DIR = Path(__file__).resolve().parent / "skill_templates" / "hf-cli"
STANDARD_RUNTIME_SKILL_DIRS = (
    XURL_SKILL_DIR,
    HF_CLI_SKILL_DIR,
    SECURE_CREDENTIAL_WORKFLOWS_SKILL_DIR,
    RUNTIME_OPERATIONS_WORKFLOWS_SKILL_DIR,
    SHELL_EXECUTION_WORKFLOWS_SKILL_DIR,
    CONVERT_DOCUMENTS_TO_MARKDOWN_SKILL_DIR,
)


def build_standard_skill_directories(
    additional_skill_directories: list[str] | None,
    *,
    skills_enabled: bool,
) -> list[str]:
    directories = list(additional_skill_directories or [])
    existing_directories = {str(directory) for directory in directories}
    if skills_enabled:
        for skill_dir in STANDARD_RUNTIME_SKILL_DIRS:
            skill_dir_text = str(skill_dir)
            if skill_dir_text not in existing_directories:
                directories.append(skill_dir_text)
                existing_directories.add(skill_dir_text)
    return directories


def build_agent_skill_directories(
    additional_skill_directories: list[str] | None,
    *,
    skills_enabled: bool,
    named_agent_type: str | None,
) -> list[str]:
    """Add packaged skills shared by all agents and manager-only disciplines."""

    directories = build_standard_skill_directories(
        additional_skill_directories,
        skills_enabled=skills_enabled,
    )
    if skills_enabled and named_agent_type == TEAM_ARCHITECT_AGENT_TYPE:
        long_horizon_skill_dir = str(LONG_HORIZON_PLAN_EXECUTION_SKILL_DIR)
        if long_horizon_skill_dir not in directories:
            directories.append(long_horizon_skill_dir)
    return directories


_logger = logging.getLogger(__name__)


@dataclass
class _SpawnedAgentHandle:
    agent: Any
    thread: threading.Thread
    team_id: str
    runtime_loop: asyncio.AbstractEventLoop | None = None


_SPAWNED_AGENT_LOCK = threading.Lock()
_SPAWNED_AGENTS: dict[str, _SpawnedAgentHandle] = {}


async def worker_holds_work(agent: Any) -> bool:
    """Whether an idle-timed worker still holds work and must not exit.

    Any unfinished task of its own counts, waiting and paused ones and tasks
    delegated to it included, and so do messages in its deferred inbox. Before
    2026-10-04 only runnable top-level tasks counted, so a worker exited after
    ten idle minutes while its task waited on an access decision, a question or
    a subtask (whose replies are addressed to this worker), leaving the task
    orphaned; and a task delegated to it never counted at all. The stuck-task
    check tells the person when such a wait lasts long.
    """
    manager = agent.task_manager
    if manager is None:
        return False
    if manager.current_task_id or getattr(agent, "deferred_inbox", None):
        return True
    return bool(await manager.operations_runtime.run(manager.has_unfinished_local_tasks))


def inspect_worker_launch(launch: AgentLaunchV1) -> str:
    """Host evidence for one start: "absent", "starting", "ready" or "unknown".

    A start recorded by another process is "absent" only when that process is
    provably gone (``runtime_hosting.process_run``); otherwise its threads
    cannot be inspected from here and the answer is "unknown".
    """
    from custom_agents.agent_instantiation_service import PROCESS_HOST_ID
    from runtime_hosting.process_run import (
        RecordedProcessState,
        current_process_identity,
        current_run_proven,
        recorded_process_state,
    )

    if launch.host_id != PROCESS_HOST_ID:
        state = recorded_process_state(
            launch.host_id,
            current=current_process_identity(),
            run_proven=current_run_proven(),
        )
        return "absent" if state is RecordedProcessState.GONE else "unknown"
    with _SPAWNED_AGENT_LOCK:
        handle = _SPAWNED_AGENTS.get(launch.agent_id)
    if handle is None or not handle.thread.is_alive():
        return "absent"
    if handle.team_id != launch.team_id or handle.agent._worker_launch.launch_id != launch.launch_id:
        return "unknown"
    return "ready" if handle.agent._worker_ready else "starting"


def stop_worker_launch(launch: AgentLaunchV1) -> bool:
    from custom_agents.agent_instantiation_service import PROCESS_HOST_ID

    if launch.host_id != PROCESS_HOST_ID:
        return False
    with _SPAWNED_AGENT_LOCK:
        handle = _SPAWNED_AGENTS.get(launch.agent_id)
    if handle is None:
        return True
    # A startup timeout can retire an unready child. Admitted work needs the
    # separate task cancellation protocol and must never be killed here.
    if handle.agent._worker_ready:
        return False
    _request_spawned_agent_exits([handle], team_id=launch.team_id)
    handle.thread.join(timeout=1.0)
    return not handle.thread.is_alive()


def _register_spawned_agent(handle: _SpawnedAgentHandle) -> None:
    with _SPAWNED_AGENT_LOCK:
        _SPAWNED_AGENTS[handle.agent.id] = handle


def _unregister_spawned_agent(agent_id: str) -> None:
    with _SPAWNED_AGENT_LOCK:
        _SPAWNED_AGENTS.pop(agent_id, None)


def _spawned_agent_handles(
    *,
    team_id: str,
    owner_agent_id: str,
) -> list[_SpawnedAgentHandle]:
    with _SPAWNED_AGENT_LOCK:
        return [
            handle
            for handle in _SPAWNED_AGENTS.values()
            if handle.team_id == team_id and handle.agent.id != owner_agent_id
        ]


def _request_spawned_agent_exits(
    handles: list[_SpawnedAgentHandle],
    *,
    team_id: str,
) -> list[tuple[_SpawnedAgentHandle, Any]]:
    exit_futures: list[tuple[_SpawnedAgentHandle, Any]] = []
    for handle in handles:
        try:
            if handle.runtime_loop is None or not handle.runtime_loop.is_running():
                handle.agent.set_exit_flag()
                continue
            exit_coroutine = handle.agent.exit_agent()
            try:
                future = asyncio.run_coroutine_threadsafe(
                    exit_coroutine,
                    handle.runtime_loop,
                )
            except Exception:
                exit_coroutine.close()
                raise
            exit_futures.append((handle, future))
        except Exception:
            _logger.exception(
                "Could not request spawned agent shutdown. "
                "event=spawned_agent_shutdown_request_failed agent_id=%s team_id=%s",
                handle.agent.id,
                team_id,
            )
    return exit_futures


def _await_spawned_agent_exit_requests(
    exit_futures: list[tuple[_SpawnedAgentHandle, Any]],
    *,
    deadline: float,
    team_id: str,
) -> None:
    for handle, future in exit_futures:
        remaining = max(0.0, deadline - time.monotonic())
        if remaining <= 0:
            return
        try:
            future.result(timeout=remaining)
        except Exception as exc:
            _logger.warning(
                "Spawned agent exit request did not complete before join. "
                "event=spawned_agent_shutdown_request_incomplete agent_id=%s "
                "team_id=%s error=%s",
                handle.agent.id,
                team_id,
                exc,
            )


def _join_spawned_agent_threads(
    handles: list[_SpawnedAgentHandle],
    *,
    deadline: float,
    timeout_seconds: float,
    team_id: str,
) -> None:
    for handle in handles:
        remaining = max(0.0, deadline - time.monotonic())
        if remaining <= 0:
            break
        handle.thread.join(timeout=remaining)
    still_running = [handle.agent.id for handle in handles if handle.thread.is_alive()]
    if still_running:
        _logger.warning(
            "Spawned agents did not stop within the graceful shutdown budget. "
            "event=spawned_agent_shutdown_timeout team_id=%s timeout_seconds=%s "
            "agent_ids=%s",
            team_id,
            timeout_seconds,
            still_running,
        )


def shutdown_spawned_agents(
    team_id: str,
    owner_agent_id: str,
    timeout_seconds: float,
) -> None:
    """Request clean exit for spawned agents and wait for their threads."""

    handles = _spawned_agent_handles(
        team_id=team_id,
        owner_agent_id=owner_agent_id,
    )
    if not handles:
        return

    deadline = time.monotonic() + max(0.0, float(timeout_seconds))
    exit_futures = _request_spawned_agent_exits(handles, team_id=team_id)
    _await_spawned_agent_exit_requests(
        exit_futures,
        deadline=deadline,
        team_id=team_id,
    )
    _join_spawned_agent_threads(
        handles,
        deadline=deadline,
        timeout_seconds=timeout_seconds,
        team_id=team_id,
    )


def _start_spawned_agent(handle: _SpawnedAgentHandle) -> None:
    _register_spawned_agent(handle)
    try:
        handle.thread.start()
    except Exception:
        _unregister_spawned_agent(handle.agent.id)
        raise


def _transport_agent_kwargs(config: dict[str, Any]) -> dict[str, Any]:
    """Resolve the transport settings passed through the composition root."""

    return {
        "transport_type": config.get("transport_type", TRANSPORT_DEFAULTS.type.value),
        "nats_url": config.get("nats_url", TRANSPORT_DEFAULTS.nats_url.value),
        "nats_servers": list(
            config.get("nats_servers") or TRANSPORT_DEFAULTS.nats_servers.value
        ),
        "nats_name": config.get("nats_name", TRANSPORT_DEFAULTS.nats_name.value),
        "nats_jetstream_enabled": config.get(
            "nats_jetstream_enabled",
            TRANSPORT_DEFAULTS.jetstream_enabled.value,
        ),
        "nats_subject_prefix": config.get(
            "nats_subject_prefix",
            TRANSPORT_DEFAULTS.subject_prefix.value,
        ),
        "nats_storage": config.get("nats_storage", TRANSPORT_DEFAULTS.storage.value),
        "nats_replicas": config.get("nats_replicas", TRANSPORT_DEFAULTS.replicas.value),
        "nats_ack_wait_seconds": config.get(
            "nats_ack_wait_seconds",
            TRANSPORT_DEFAULTS.ack_wait_seconds.value,
        ),
        "nats_max_deliver": config.get(
            "nats_max_deliver",
            TRANSPORT_DEFAULTS.max_deliver.value,
        ),
        "nats_duplicate_window_seconds": config.get(
            "nats_duplicate_window_seconds",
            TRANSPORT_DEFAULTS.duplicate_window_seconds.value,
        ),
        "nats_work_max_age_seconds": config.get(
            "nats_work_max_age_seconds",
            TRANSPORT_DEFAULTS.work_max_age_seconds.value,
        ),
        "nats_presence_max_age_seconds": config.get(
            "nats_presence_max_age_seconds",
            TRANSPORT_DEFAULTS.presence_max_age_seconds.value,
        ),
        "nats_process_consumer_inactive_threshold_seconds": config.get(
            "nats_process_consumer_inactive_threshold_seconds",
            TRANSPORT_DEFAULTS.process_consumer_inactive_threshold_seconds.value,
        ),
        "nats_user": config.get("nats_user", TRANSPORT_DEFAULTS.nats_user.value),
        "nats_password": config.get(
            "nats_password",
            TRANSPORT_DEFAULTS.nats_password.value,
        ),
        "nats_credentials_file": config.get(
            "nats_credentials_file",
            TRANSPORT_DEFAULTS.credentials_file.value,
        ),
    }


# Predefined mapping of agent types to hydrated runtime configurations.
# Role descriptions, prompt refs, onboarding metadata, and default tools (tool
# catalog ids) live in YAML catalogs; the catalog maps them to trusted Strands
# functions (custom_agents.agent_catalog).
AGENT_TYPE_MAP: Dict[str, Dict] = load_agent_type_map()
# Tools list the catalogs as they are now, so a specialist TeamLead creates is
# listed at once; launches read them the same way (resolve_definition below).
configure_agent_type_source(load_agent_type_map)


def create_strands_agent(config: dict):
    """
    Creates and configures a Strands-backed UniversalAgent.

    Args:
        config: Dictionary containing agent configuration parameters.

    Returns:
        An initialized UniversalAgent instance.

    Raises:
        ValueError: If required configuration is missing.
        ImportError: If the agent class cannot be imported.
    """
    from custom_agents.strands_agent import UniversalAgent
    from custom_agents.task_manager import TaskManager
    from custom_agents.generated_agent_runtime import (
        apply_generated_agent_provisioning,
    )
    from custom_agents.providers.mcp_connections import (
        CONFIGURED_CONNECTION_IDS,
        apply_mcp_connection_reconciliation,
    )

    # Ensure team_id is set
    if not config.get("team_id"):
        raise ValueError("Configuration missing required 'team_id'")
        # import uuid
        # config['team_id'] = 'team_<12 lowercase hex characters>'

    config = apply_generated_agent_provisioning(config)
    # Configured MCP servers are desired configuration, not connection
    # authority. Reconcile them so the agent receives only the ids of their
    # installed records; it loads the active ones before model calls
    # (custom_agents.tool_sync), never at startup.
    config = apply_mcp_connection_reconciliation(config)
    tools = list(config.get("tools", []) or [])
    authorization_config = dict(config.get("authorization_config", {}) or {})
    authorization_on = authorization_enabled(authorization_config)
    from custom_agents.skill_installation import SkillInstallationSettings

    installation_settings = SkillInstallationSettings.from_mapping(
        config.get("skill_installation_config")
    )
    skill_installation_enabled = bool(config.get("skills_enabled", False)) and (
        installation_settings.enabled
    )
    # Shell and python_env read the sandbox settings from the process
    # environment; the resolved config already includes environment overrides.
    if config.get("code_sandbox_mode"):
        os.environ[ENV_CODE_SANDBOX_MODE] = str(config.get("code_sandbox_mode"))
        os.environ[ENV_CODE_SANDBOX_EXTRA_READ_PATHS] = json.dumps(
            list(config.get("code_sandbox_extra_read_paths") or [])
        )
        os.environ[ENV_CODE_SANDBOX_EXTRA_WRITE_PATHS] = json.dumps(
            list(config.get("code_sandbox_extra_write_paths") or [])
        )
    if config.get("secure_tool_broker_internal_base_url"):
        os.environ["SECURE_TOOL_BROKER_INTERNAL_BASE_URL"] = str(
            config.get("secure_tool_broker_internal_base_url")
        )
    if config.get("network_public_origin"):
        os.environ["NETWORK_PUBLIC_ORIGIN"] = str(config.get("network_public_origin"))
    if config.get("secure_tool_broker_public_port") is not None:
        os.environ["SECURE_TOOL_BROKER_PUBLIC_PORT"] = str(
            config.get("secure_tool_broker_public_port")
        )
    if config.get("internal_website_base_url"):
        os.environ["INTERNAL_WEBSITE_BASE_URL"] = str(
            config.get("internal_website_base_url")
        )
    if config.get("instance_root"):
        # Tool path checks and the code sandbox read the root from the process
        # environment when no agent context is available (for example in
        # helper threads); every agent in this process shares one instance.
        os.environ["TEAM_INSTANCE_ROOT"] = str(config.get("instance_root"))
    tool_filter = {
        "skill_installation_enabled": skill_installation_enabled,
        "tool_allowlist": config.get("tool_allowlist"),
        "tool_blocklist": config.get("tool_blocklist"),
    }
    tools = filter_agent_tools(
        tools,
        authorization_enabled=authorization_on,
        skill_installation_enabled=skill_installation_enabled,
        tool_allowlist=config.get("tool_allowlist"),
        tool_blocklist=config.get("tool_blocklist"),
    )
    if not authorization_on:
        omitted_access_tools = sorted(
            {
                tool_name(tool)
                for tool in config.get("tools", []) or []
                if tool_name(tool) in HIDDEN_WITHOUT_AUTHORIZATION
            }
        )
        _logger.info(
            "Authorization is off, so access tools are left out of this agent. "
            "event=authorization_off_agent_tools_omitted agent_type=%s "
            "omitted_tools=%s",
            config.get("named_agent_type", "unknown"),
            omitted_access_tools,
        )
    config["tools"] = tools

    # Ensure tools list exists
    if not config.get("tools"):
        _logger.warning(
            f"No tools defined for agent {config.get('named_agent_type', 'unknown')}. Initializing with empty list."
        )
        config["tools"] = []

    raw_backstory = config.get("backstory", "")
    if not authorization_on:
        raw_backstory = strip_access_prompt(raw_backstory)
    filled_backstory = fill_backstory_template(raw_backstory, config["team_id"])
    # Build system prompt from components
    system_prompt = build_system_prompt(
        role=config.get("role", ""),
        goal=config.get("goal", ""),
        backstory=raw_backstory,
        team_id=config["team_id"],
    )

    additional_skill_directories = build_agent_skill_directories(
        config.get("additional_skill_directories", []) or [],
        skills_enabled=bool(config.get("skills_enabled", False)),
        named_agent_type=config.get("named_agent_type"),
    )
    if config.get("skills_enabled", False):
        from custom_agents.skill_installation import (
            managed_skill_directory_for_runtime_config,
        )

        if installation_settings.enabled:
            managed_skills_dir = str(managed_skill_directory_for_runtime_config(config))
            if managed_skills_dir not in additional_skill_directories:
                additional_skill_directories.append(managed_skills_dir)

    if (
        config.get("skills_enabled", False)
        and config.get("named_agent_type") == AGENT_DEFAULTS.named_agent_type.value
    ):
        from config_loader import get_config

        additional_skill_directories.append(
            str(
                ensure_runtime_agent_instantiation_skill(
                    team_id=config["team_id"],
                    agent_type_map=AGENT_TYPE_MAP,
                    mcp_profiles=get_config().to_dict().get("mcp_profiles"),
                    current_agent_type=config.get(
                        "named_agent_type", AGENT_DEFAULTS.named_agent_type.value
                    ),
                    instance_root=config.get("instance_root"),
                    team_templates_path=config.get("team_templates_path"),
                    team_composition_path=config.get("team_composition_path"),
                    authorization_enabled=authorization_on,
                )
            )
        )

    plugins = list(config.get("plugins", []) or [])
    if config.get("skills_enabled", False):
        plugins.append(
            build_skills_plugin(
                skills_dir=config.get("skills_dir"),
                additional_skill_directories=additional_skill_directories,
                excluded_skill_names=config.get("prompt_skill_description_exclusions"),
                prompt_skill_description_limit=config.get(
                    "prompt_skill_description_limit",
                    AGENT_DEFAULTS.prompt_skill_description_limit.value,
                )
                or AGENT_DEFAULTS.prompt_skill_description_limit.value,
            )
        )

    instance_root = config.get("instance_root")
    workspace_dir = config.get("workspace_dir")
    llm_override_path = config.get("llm_override_path")
    team_templates_path = config.get("team_templates_path")
    team_composition_path = config.get("team_composition_path")
    if instance_root:
        layout = InstanceLayout.at(instance_root)
        workspace_dir = workspace_dir or str(layout.workspaces_dir)
        llm_override_path = llm_override_path or str(layout.llm_overrides_path)
        team_templates_path = team_templates_path or str(layout.team_templates_path)
        team_composition_path = team_composition_path or str(layout.team_composition_path)
        system_prompt = f"{system_prompt}\n\n{build_workspace_prompt_context(layout)}"

    # Create agent instance
    task_history_db_path = config.get(
        "task_history_db_path",
        STORAGE_DEFAULTS.task_history_db_path.value,
    )
    storage_settings = resolve_storage_settings(
        {
            "backend": config.get("storage_backend", STORAGE_DEFAULTS.backend.value),
            STORAGE_BACKEND_SQLITE: {
                "path": config.get("storage_sqlite_path") or task_history_db_path,
                "vector": {
                    "enabled": config.get(
                        "storage_sqlite_vector_enabled",
                        STORAGE_DEFAULTS.sqlite_vector_enabled.value,
                    ),
                    "provider": config.get(
                        "storage_sqlite_vector_provider",
                        STORAGE_DEFAULTS.sqlite_vector_provider.value,
                    ),
                    "require_extension": config.get(
                        "storage_sqlite_vector_require_extension",
                        STORAGE_DEFAULTS.sqlite_vector_require_extension.value,
                    ),
                },
            },
            STORAGE_BACKEND_POSTGRES: {"dsn": config.get("storage_postgres_dsn")},
        },
        base_dir=instance_root,
        sqlite_fallback_path=task_history_db_path,
    )
    agent_kwargs = {
        "system_prompt": system_prompt,
        "role": config.get("role", ""),
        "goal": config.get("goal", ""),
        "backstory": filled_backstory,
        "team_id": config["team_id"],
        "is_team_lead": config.get("is_team_lead", False),
        "access_role": str(config.get("access_role") or "worker"),
        **_transport_agent_kwargs(config),
        "tools": list(config.get("tools", [])),
        "default_tools": tuple(config.get("default_tools") or ()),
        "tool_filter": tool_filter,
        "plugins": plugins,
        "skills_dir": config.get("skills_dir"),
        "skill_installation_config": dict(
            config.get("skill_installation_config", {}) or {}
        ),
        "instance_root": instance_root,
        "workspace_dir": workspace_dir,
        "instance_config_path": config.get("instance_config_path"),
        "bootstrap_config": dict(config.get("bootstrap_config", {}) or {}),
        "authorization_config": authorization_config,
        "scratch_retention_days": int(
            config.get(
                "scratch_retention_days",
                TOOL_POLICY_DEFAULTS.scratch_retention_days.value,
            )
        ),
        "mcp_config": dict(config.get("mcp_config", config.get("mcp", {})) or {}),
        CONFIGURED_CONNECTION_IDS: list(config.get(CONFIGURED_CONNECTION_IDS) or []),
        "llm_override_path": llm_override_path,
        "team_templates_path": team_templates_path,
        "team_composition_path": team_composition_path,
        "additional_skill_directories": additional_skill_directories,
        "skill_exclusions": list(
            config.get("prompt_skill_description_exclusions", []) or []
        ),
        "prompt_skill_description_limit": config.get(
            "prompt_skill_description_limit",
            AGENT_DEFAULTS.prompt_skill_description_limit.value,
        )
        or AGENT_DEFAULTS.prompt_skill_description_limit.value,
        "model_provider": config.get("model_provider", LLM_DEFAULTS.provider.value),
        "model_id": config.get("model_id", LLM_DEFAULTS.model_id.value),
        "temperature": config.get("temperature", LLM_DEFAULTS.temperature.value),
        "reasoning_effort": config.get("reasoning_effort"),
        "reasoning_summary": config.get("reasoning_summary"),
        "max_output_tokens": config.get(
            "max_output_tokens", LLM_DEFAULTS.max_output_tokens.value
        ),
        "context_window_tokens": config.get("context_window_tokens"),
        "stream": config.get("stream", LLM_DEFAULTS.stream.value),
        "litellm_api_mode": config.get(
            "litellm_api_mode", LLM_DEFAULTS.litellm_api_mode.value
        ),
        "request_timeout_seconds": config.get(
            "request_timeout_seconds",
            LLM_DEFAULTS.request_timeout_seconds.value,
        ),
        "stream_idle_timeout_seconds": config.get(
            "stream_idle_timeout_seconds",
            LLM_DEFAULTS.stream_idle_timeout_seconds.value,
        ),
        "model_call_timeout_seconds": config.get(
            "model_call_timeout_seconds",
            LLM_DEFAULTS.model_call_timeout_seconds.value,
        ),
        "supports_image_input": config.get(
            "supports_image_input",
            LLM_DEFAULTS.supports_image_input.value,
        ),
        "supports_audio_input": config.get(
            "supports_audio_input",
            LLM_DEFAULTS.supports_audio_input.value,
        ),
        "supports_file_input": config.get(
            "supports_file_input",
            LLM_DEFAULTS.supports_file_input.value,
        ),
        "reasoning_trace_retention_messages": config.get(
            "reasoning_trace_retention_messages",
            LLM_DEFAULTS.reasoning_trace_retention_messages.value,
        ),
        "compaction_model_provider": config.get("compaction_model_provider"),
        "compaction_model_id": config.get("compaction_model_id"),
        "llm_failover": dict(
            config.get("llm_failover", LLM_DEFAULTS.failover.value) or {}
        ),
        "conversation_compactor_type": config.get(
            "conversation_compactor_type",
            CONVERSATION_COMPACTION_DEFAULTS.compactor_type.value,
        ),
        "memory_graph_enabled": config.get(
            "memory_graph_enabled", AGENT_DEFAULTS.memory_graph_enabled.value
        ),
        "memory_config": dict(
            config.get("memory_config", AGENT_DEFAULTS.memory.value) or {}
        ),
        "knowledge_config": dict(config.get("knowledge_config") or {}),
        "schedule_workers": dict(config.get("schedule_workers", AGENT_DEFAULTS.schedule_workers.value)),
        "worker_pools": dict(config.get("worker_pools", AGENT_DEFAULTS.worker_pools.value)),
        "schedule_maintenance": dict(
            config.get(
                "schedule_maintenance", AGENT_DEFAULTS.schedule_maintenance.value
            )
            or {}
        ),
        "embedding_provider": config.get(
            "embedding_provider", EMBEDDING_DEFAULTS.provider.value
        ),
        "embedding_model_id": config.get("embedding_model_id"),
        "embedding_api_base": config.get("embedding_api_base"),
        "embedding_dimensions": config.get("embedding_dimensions"),
        "embedding_timeout_seconds": config.get(
            "embedding_timeout_seconds",
            EMBEDDING_DEFAULTS.timeout_seconds.value,
        ),
        "embedding_organization": config.get("embedding_organization"),
        "embedding_extra_headers": dict(
            config.get("embedding_extra_headers", {}) or {}
        ),
        "named_agent_type": config.get("named_agent_type"),
        "task_manager": TaskManager(
            max_queued_top_level_tasks=config.get(
                "max_queued_top_level_tasks",
                AGENT_DEFAULTS.max_queued_top_level_tasks.value,
            ),
            queue_warning_threshold=config.get(
                "task_queue_warning_threshold",
                AGENT_DEFAULTS.task_queue_warning_threshold.value,
            ),
            db_path=task_history_db_path,
            logger=logging.getLogger("TaskManager"),
            storage_settings=storage_settings,
            objective_execution_paths=(
                config.get("objective_execution_paths")
                if config.get("named_agent_type") == TEAM_ARCHITECT_AGENT_TYPE
                else []
            ),
        ),
        "default_request_timezone": config.get("default_request_timezone"),
        "subtask_timeout_seconds": config.get(
            "subtask_timeout_seconds",
            AGENT_DEFAULTS.subtask_timeout_seconds.value,
        ),
        "subtask_watchdog_interval_seconds": config.get(
            "subtask_watchdog_interval_seconds",
            AGENT_DEFAULTS.subtask_watchdog_interval_seconds.value,
        ),
        "subtask_status_probe_timeout_seconds": config.get(
            "subtask_status_probe_timeout_seconds",
            AGENT_DEFAULTS.subtask_status_probe_timeout_seconds.value,
        ),
        "restart_executing_task_grace_seconds": config.get(
            "restart_executing_task_grace_seconds",
            AGENT_DEFAULTS.restart_executing_task_grace_seconds.value,
        ),
        "same_role_runtime_takeover_enabled": config.get(
            "same_role_runtime_takeover_enabled",
            AGENT_DEFAULTS.same_role_runtime_takeover_enabled.value,
        ),
        "max_tool_calls_per_task": config.get(
            "max_tool_calls_per_task", AGENT_DEFAULTS.max_tool_calls_per_task.value
        ),
        "max_tool_result_tokens_per_call": config.get(
            "max_tool_result_tokens_per_call",
            AGENT_DEFAULTS.max_tool_result_tokens_per_call.value,
        ),
        "runtime_budget_policy": dict(
            config.get(
                "runtime_budget_policy", AGENT_DEFAULTS.runtime_budget_policy.value
            )
            or {}
        ),
        "turn_action_outbox": dict(
            config.get("turn_action_outbox", AGENT_DEFAULTS.turn_action_outbox.value)
            or {}
        ),
        "runtime_coordinator": dict(
            config.get("runtime_coordinator", AGENT_DEFAULTS.runtime_coordinator.value)
            or {}
        ),
        "inner_loop_context": dict(config.get("inner_loop_context", {}) or {}),
        "debug": dict(config.get("debug", AGENT_DEFAULTS.debug.value) or {}),
        "structured_output_failure_threshold": config.get(
            "structured_output_failure_threshold",
            AGENT_DEFAULTS.structured_output_failure_threshold.value,
        ),
        "context_engine": dict(config.get("context_engine", {}) or {}),
        "conversation_summary_generator_type": config.get(
            "conversation_summary_generator_type",
            AGENT_DEFAULTS.conversation_summary_generator_type.value,
        ),
        "deferred_message_warning_threshold": config.get(
            "deferred_message_warning_threshold",
            AGENT_DEFAULTS.deferred_message_warning_threshold.value,
        ),
        # API keys from config or environment variables
        # LiteLLM proxy settings
        "litellm_api_base": config.get("litellm_api_base"),
        "openai_codex": dict(
            config.get("openai_codex") or LLM_DEFAULTS.openai_codex.value
        ),
        "xai_oauth": dict(config.get("xai_oauth") or LLM_DEFAULTS.xai_oauth.value),
        "agent_result_transformer_spec": config.get("agent_result_transformer_spec"),
        "context_compaction_trigger_ratio": config.get(
            "context_compaction_trigger_ratio",
            CONVERSATION_COMPACTION_DEFAULTS.trigger_ratio.value,
        ),
        "context_compaction_target_ratio": config.get(
            "context_compaction_target_ratio",
            CONVERSATION_COMPACTION_DEFAULTS.target_ratio.value,
        ),
        "context_compaction_min_recent_turns": config.get(
            "context_compaction_min_recent_turns",
            CONVERSATION_COMPACTION_DEFAULTS.min_recent_turns.value,
        ),
        "context_compaction_max_recent_turns": config.get(
            "context_compaction_max_recent_turns",
            CONVERSATION_COMPACTION_DEFAULTS.max_recent_turns.value,
        ),
        "context_compaction_trigger_turn_count": config.get(
            "context_compaction_trigger_turn_count"
        ),
        "context_compaction_trigger_message_count": config.get(
            "context_compaction_trigger_message_count"
        ),
        "activity_collector_base_url": config.get("activity_collector_base_url"),
        "activity_collector_connect_timeout_seconds": config.get(
            "activity_collector_connect_timeout_seconds",
            ACTIVITY_COLLECTOR_DEFAULTS.connect_timeout_seconds.value,
        ),
        "activity_collector_request_timeout_seconds": config.get(
            "activity_collector_request_timeout_seconds",
            ACTIVITY_COLLECTOR_DEFAULTS.request_timeout_seconds.value,
        ),
        "activity_collector_batch_max_events": config.get(
            "activity_collector_batch_max_events",
            ACTIVITY_COLLECTOR_DEFAULTS.batch_max_events.value,
        ),
        "activity_collector_flush_interval_seconds": config.get(
            "activity_collector_flush_interval_seconds",
            ACTIVITY_COLLECTOR_DEFAULTS.flush_interval_seconds.value,
        ),
        "activity_collector_max_buffered_events": config.get(
            "activity_collector_max_buffered_events",
            ACTIVITY_COLLECTOR_DEFAULTS.max_buffered_events.value,
        ),
        "activity_collector_drop_when_unavailable": config.get(
            "activity_collector_drop_when_unavailable",
            ACTIVITY_COLLECTOR_DEFAULTS.drop_when_unavailable.value,
        ),
        "activity_collector_ingest_token_env_var": config.get(
            "activity_collector_ingest_token_env_var",
            ACTIVITY_COLLECTOR_DEFAULTS.ingest_token_env_var.value,
        ),
        "otel_enabled": config.get("otel_enabled", OTEL_DEFAULTS.enabled.value),
        "otel_enable_traces": config.get(
            "otel_enable_traces", OTEL_DEFAULTS.enable_traces.value
        ),
        "otel_enable_metrics": config.get(
            "otel_enable_metrics", OTEL_DEFAULTS.enable_metrics.value
        ),
        "otel_otlp_endpoint": config.get("otel_otlp_endpoint"),
        "otel_service_name": config.get("otel_service_name"),
        "otel_headers": dict(config.get("otel_headers", {}) or {}),
        # The team module is the concrete composition root. UniversalAgent sees
        # only this narrow process-local contract and never imports this module.
        "local_team_runtime": LocalAgentTeamRuntime(
            agent_types=lambda: tuple(load_agent_type_map()),
            spawn_agent=instantiate_agent_type,
            shutdown_agents=shutdown_spawned_agents,
            resolve_definition=lambda agent_type: load_agent_type_map().get(agent_type),
            inspect_launch=inspect_worker_launch,
            stop_launch=stop_worker_launch,
        ),
    }

    # Only set 'id' if explicitly provided in config to allow auto-generation otherwise
    if config.get("id"):
        agent_kwargs["id"] = config["id"]

    agent = UniversalAgent(**agent_kwargs)

    # NOTE: Callers must call set_agent_ref(agent) in their own execution context
    # (main thread or spawned thread) so tools have access to the correct agent.
    # We don't call it here because:
    # 1. In multi-agent scenarios, each call would overwrite the previous
    # 2. If using instantiate_agent_type(), the agent runs in a separate thread
    #    which needs its own context set there

    return agent


def instantiate_agent_type(
    named_agent_type: str,
    team_id: str,
    is_team_lead: bool = False,
    mcp_profile_names: list[str] | None = None,
    idle_ttl_seconds: float | None = None,
    *,
    launch: AgentLaunchV1,
    runtime_config: Any,
) -> AgentLaunchResultV1:
    """
    Instantiates a UniversalAgent of the specified type and starts it in a background thread.

    Args:
        named_agent_type: The type of agent to instantiate (e.g., "CodeWritingAgent").
        team_id: The team ID for this agent.
        is_team_lead: Whether this agent should act as the team leader.
        idle_ttl_seconds: Optional idle TTL for dynamically spawned ephemeral agents.

    Returns:
        A typed startup acknowledgement. Readiness and admission are separate.
    """
    from custom_agents.agent_instantiation_service import update_runtime_launch
    from custom_agents.schedule_definition_identity import launch_definition_digest

    config = load_agent_type_map().get(named_agent_type)

    if not config:
        raise ValueError("agent_definition_unavailable")
    if launch_definition_digest(config) != launch.definition_digest:
        raise ValueError("agent_definition_changed_before_construction")
    if (launch.named_agent_type, launch.team_id) != (named_agent_type, team_id) or is_team_lead:
        raise ValueError("launch_identity_mismatch")
    if idle_ttl_seconds is not None:
        try:
            idle_ttl_seconds = float(idle_ttl_seconds)
        except (TypeError, ValueError):
            raise ValueError("idle_ttl_seconds must be a positive number") from None
        if idle_ttl_seconds <= 0:
            raise ValueError("idle_ttl_seconds must be a positive number")

    # Copy config to avoid modifying the original
    agent_config = config.copy()
    agent_config["team_id"] = team_id
    agent_config["is_team_lead"] = is_team_lead
    agent_config["id"] = launch.agent_id
    from config_loader import resolve_agent_type_settings

    shared_config = runtime_config
    agent_config.update(resolve_agent_type_settings(shared_config, named_agent_type))
    if not is_team_lead:
        # Bootstrap identity belongs only to TeamArchitect. Scheduled workers receive
        # the relevant canonical owner through their manager-authored task metadata.
        agent_config["bootstrap_config"] = {}
    _logger.debug(
        "event=agent_spawn_requested named_agent_type=%s team_id=%s is_team_lead=%s "
        "mcp_profile_names=%s idle_ttl_seconds=%s model_provider=%s model_id=%s nats_servers=%s storage_backend=%s",
        named_agent_type,
        team_id,
        is_team_lead,
        list(mcp_profile_names or []),
        idle_ttl_seconds,
        agent_config.get("model_provider"),
        agent_config.get("model_id"),
        agent_config.get("nats_servers"),
        shared_config.get("storage.backend"),
    )
    try:
        profile_server_configs = resolve_mcp_profile_configs(
            mcp_profile_names,
            shared_config.to_dict().get("mcp_profiles"),
        )
    except ValueError as exc:
        raise ValueError("MCP launch profile unavailable") from exc
    agent_config["external_mcp_servers"] = merge_external_mcp_server_configs(
        agent_config.get("external_mcp_servers"),
        profile_server_configs,
    )

    # Create the agent (but don't set agent_ref here - it needs to be in the thread)
    agent = create_strands_agent(agent_config)
    agent._worker_launch = launch
    _logger.debug(
        "event=agent_spawn_created agent_id=%s agent_name=%s named_agent_type=%s team_id=%s",
        agent.id,
        agent.name,
        named_agent_type,
        team_id,
    )
    thread_name = f"agent-{named_agent_type}-{agent.id[-8:]}"

    def _run_agent_in_thread():
        """Target function for the thread to run the agent's async connect."""

        async def _idle_ttl_monitor() -> None:
            # Exits the worker after idle_ttl_seconds without work, and at once
            # when the worker pool controller asked it to drain and it holds no
            # work. Runs for every spawned worker; without a TTL only the drain
            # applies.
            last_busy_at = time.monotonic()
            check_interval = (
                max(1.0, min(10.0, idle_ttl_seconds / 4.0)) if idle_ttl_seconds else 5.0
            )
            while not agent.get_exit_event().is_set():
                # A worker that lost its lease exits through its wind-down,
                # which may still hold tasks waiting on a person or subtask.
                if not agent._worker_ready or is_winding_down(agent):
                    last_busy_at = time.monotonic()
                    await asyncio.sleep(check_interval)
                    continue
                try:
                    busy = await worker_holds_work(agent)
                except Exception:
                    _logger.exception("Cannot establish worker idleness", extra={"agent_id": agent.id})
                    last_busy_at = time.monotonic()
                    await asyncio.sleep(check_interval)
                    continue
                if not busy and getattr(agent, "_pool_draining", False):
                    _logger.info(
                        "A worker the pool no longer needs is leaving. "
                        "event=worker_pool_drained_exit agent_id=%s named_agent_type=%s team_id=%s",
                        agent.id,
                        named_agent_type,
                        team_id,
                    )
                    await agent.exit_agent()
                    return
                if busy:
                    last_busy_at = time.monotonic()
                elif idle_ttl_seconds is not None and time.monotonic() - last_busy_at >= idle_ttl_seconds:
                    _logger.info(
                        "event=agent_idle_ttl_expired agent_id=%s named_agent_type=%s team_id=%s idle_ttl_seconds=%s",
                        agent.id,
                        named_agent_type,
                        team_id,
                        idle_ttl_seconds,
                    )
                    await agent.exit_agent()
                    return
                await asyncio.sleep(check_interval)

        async def _connect_with_optional_idle_ttl() -> None:
            agent_handle.runtime_loop = asyncio.get_running_loop()
            connect_task = asyncio.create_task(agent.connect())
            ttl_task = asyncio.create_task(_idle_ttl_monitor())
            done, _pending = await asyncio.wait(
                {connect_task, ttl_task},
                return_when=asyncio.FIRST_COMPLETED,
            )
            if ttl_task in done and not connect_task.done():
                await ttl_task
                await connect_task
                return
            ttl_task.cancel()
            await asyncio.gather(ttl_task, return_exceptions=True)
            await connect_task

        try:
            _logger.debug(
                "event=agent_thread_connect_start agent_id=%s named_agent_type=%s team_id=%s thread_name=%s",
                agent.id,
                named_agent_type,
                team_id,
                threading.current_thread().name,
            )
            # Set agent reference inside the thread so tools have correct context
            set_agent_ref(agent)
            asyncio.run(_connect_with_optional_idle_ttl())
        except Exception:
            _logger.exception(
                "Spawned agent thread crashed before clean shutdown. "
                "event=agent_thread_crash agent_id=%s named_agent_type=%s team_id=%s thread_name=%s",
                agent.id,
                named_agent_type,
                team_id,
                threading.current_thread().name,
            )
        finally:
            try:
                update_runtime_launch(agent, state=AgentLaunchState.RETIRED if agent._worker_ready else AgentLaunchState.FAILED)
            except Exception:
                _logger.exception("Could not record worker exit", extra={"launch_id": launch.launch_id, "agent_id": agent.id})
            _unregister_spawned_agent(agent.id)
            _logger.debug(
                "event=agent_thread_exit agent_id=%s named_agent_type=%s team_id=%s thread_name=%s",
                agent.id,
                named_agent_type,
                team_id,
                threading.current_thread().name,
            )

    # Start agent in a background thread
    agent_thread = threading.Thread(
        target=_run_agent_in_thread, daemon=True, name=thread_name
    )
    agent_handle = _SpawnedAgentHandle(
        agent=agent,
        thread=agent_thread,
        team_id=team_id,
    )
    _start_spawned_agent(agent_handle)

    return AgentLaunchResultV1(status="starting", agent_id=agent.id, launch_id=launch.launch_id)
