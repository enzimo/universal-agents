"""Build a Strands agent with its hooks and its own tool state (``custom_agents.strands_parts``).

Each running turn calls a Strands agent of its own: the agent's main one, or
one built for a turn that runs beside it (a parallel thread's lane,
``custom_agents.execution_lanes``). The hooks are built per Strands agent and
find their task from the turn's work record, never from agent-wide state.

Kept outside ``UniversalAgent``, whose size is capped
(``tests/test_runtime_architecture_boundaries.py``). ``agent_class`` is the
Strands ``Agent`` class the caller names, so tests that replace it on
``custom_agents.strands_agent`` still replace it here.
"""

from __future__ import annotations

import inspect
from typing import Any, Dict, Optional

from custom_agents.runtime_paths import agent_layout
from custom_agents.context_debug_dump import register_context_debug_model_observer
from custom_agents.person_model_runtime import PersonModelHook
from custom_agents.runtime_guardrails import RuntimeGuardrailsPlugin
from custom_agents.default_tools import builtin_tool_catalog
from custom_agents.sdk.strands.changes_first import ChangesFirstToolExecutor
from custom_agents.strands_parts import StrandsParts
from custom_agents.task_steering_runtime import RuntimeSteeringPlugin
from custom_agents.tool_loading import LoadedTools
from custom_agents.tool_result_offloader import TaskAwareToolResultOffloader


def build_strands_parts(agent: Any, *, agent_class: Any, loaded: Optional[LoadedTools] = None) -> StrandsParts:
    """A Strands agent with the agent's model, tools and hooks, and its own tool state (module docstring).

    ``loaded`` carries the groups loaded for the current task over to a
    rebuilt main agent.
    """

    model = agent._strands_model or agent._configure_model()
    agent._strands_model = model
    agent._refresh_skills_plugins(force=True)

    # Prepare the full system prompt with agent status placeholder
    full_system_prompt = agent.system_prompt
    guardrails = RuntimeGuardrailsPlugin(
        structured_output_failure_threshold=agent.structured_output_failure_threshold,
        max_tool_calls=agent.max_tool_calls_per_task,
        logger_=agent._logger,
    )
    from custom_agents.access_runtime import AccessDecisionHook

    # Always installed: it decides every function tool call, and with
    # authorization off it allows every catalog tool (access_runtime).
    # It also withdraws access requests of tasks that end first.
    access_hook = AccessDecisionHook(agent)
    from custom_agents.tool_sync import install_tool_sync, startup_tools

    # With authorization on the model sees its role's standard tools at
    # first; the rest follow the grants valid for each task's person
    # (access Phase 7). In both modes installed MCP servers load before
    # model calls, not at startup (custom_agents.tool_sync).
    model_tools = startup_tools(agent, list(agent.tools))
    parts = StrandsParts(
        agent=None,
        loaded=loaded if loaded is not None else LoadedTools(),
        sync=None,
        generation=agent._strands_generation,
    )
    tool_sync = install_tool_sync(agent, model_tools, parts)
    parts.sync = tool_sync
    from custom_agents.access_runtime import runtime_access
    from custom_agents.tool_loading import OnRequestToolHook, ToolLimitHook, loadable_tools_text

    # The groups that load on request, listed by name and purpose, cost
    # far less than their definitions in every call (custom_agents.tool_loading).
    loadable_text = loadable_tools_text(agent, runtime_access(agent).catalog)
    if loadable_text:
        full_system_prompt = f"{full_system_prompt}\n\n{loadable_text}"
    tool_result_offloader = TaskAwareToolResultOffloader(
        max_tokens_per_call=agent.max_tool_result_tokens_per_call,
        model=model,
        artifact_root=agent_layout(agent).tool_results_dir,
        task_manager=agent.task_manager,
        team_id=agent.team_id,
        agent_id=agent.id,
        named_agent_type=agent.named_agent_type,
        logger_=agent._logger,
    )
    conversation_manager = (
        agent._context_engine.build_inner_loop_conversation_manager(
            tool_result_offloader.storage
        )
        if agent._context_engine is not None
        else None
    )
    agent_signature = inspect.signature(agent_class)
    retry_strategy_config = agent._model_failover_retry_strategy_config()
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
        tool_result_offloader,
        guardrails,
        RuntimeSteeringPlugin(agent),
        access_hook,
        tool_sync,
        PersonModelHook(agent),
        ToolLimitHook(agent, parts),
        OnRequestToolHook(agent, parts),
    ]
    if "plugins" in agent_signature.parameters:
        agent_kwargs["plugins"] = list(agent.plugins or [])
    elif agent.plugins:
        agent_hooks.extend(agent.plugins)

    # Create Strands Agent. Record changes run first in each model
    # step, and a failed one skips the step's later calls
    # (custom_agents.sdk.strands.changes_first).
    parts.agent = agent_class(
        system_prompt=full_system_prompt,
        tools=model_tools,
        model=model,
        conversation_manager=conversation_manager,
        callback_handler=None,
        hooks=agent_hooks,
        tool_executor=ChangesFirstToolExecutor(builtin_tool_catalog().record_kind),
        **agent_kwargs,
    )
    if agent._context_debug_dump_plugin is not None:
        register_context_debug_model_observer(
            parts.agent,
            agent._context_debug_dump_plugin,
            logger=agent._logger,
        )
    return parts
