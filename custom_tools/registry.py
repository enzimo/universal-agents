"""Declarative composition of trusted Strands tool providers.

Every trusted Strands function belongs to exactly one provider here, and every
one has an access level in the tool catalog (``ua_definitions/access/tools``).
Which functions an agent is offered is not decided here: agent definitions
name catalog tools (``default_tools``), the catalog maps them to functions
(``access_catalog.tool_functions``), and with authorization on the model's
tool list follows the agent's grants (``custom_agents.tool_sync``).
"""

from __future__ import annotations

from custom_tools.calculator import calculator
from custom_tools.exa_tools import exa_get_contents, exa_search
from custom_tools.local_media_context_tool import load_local_media_for_model_context
from custom_tools.non_interactive_file_tools import file_move, file_read, file_write
from custom_tools.non_interactive_shell import shell
from custom_tools.provider_contract import ToolProvider, dedupe_tools, tool_name
from custom_tools.providers.access import PROVIDER as ACCESS_PROVIDER
from custom_tools.providers.access_proposals import PROVIDER as ACCESS_PROPOSALS_PROVIDER
from custom_tools.providers.access_workflows import PROVIDER as ACCESS_WORKFLOWS_PROVIDER
from custom_tools.providers.agent_management import PROVIDER as AGENT_MANAGEMENT_PROVIDER
from custom_tools.providers.capabilities import PROVIDER as CAPABILITIES_PROVIDER
from custom_tools.providers.capabilities import TEAM_SKILL_PLANS_PROVIDER
from custom_tools.providers.changes import PROVIDER as CHANGES_PROVIDER
from custom_tools.providers.communication import PROVIDER as COMMUNICATION_PROVIDER
from custom_tools.providers.context_memory import CONVERSATIONS_PROVIDER, OWN_CONTEXT_PROVIDER
from custom_tools.providers.memory import PROVIDER as MEMORY_PROVIDER
from custom_tools.providers.diagnostics import PROVIDER as DIAGNOSTICS_PROVIDER
from custom_tools.providers.execution_control import PROVIDER as EXECUTION_CONTROL_PROVIDER
from custom_tools.providers.execution_plan import PROVIDER as EXECUTION_PLAN_PROVIDER
from custom_tools.providers.heartbeat import PROVIDER as HEARTBEAT_PROVIDER
from custom_tools.providers.knowledge import PROVIDER as KNOWLEDGE_PROVIDER
from custom_tools.providers.knowledge_curation import PROVIDER as KNOWLEDGE_CURATION_PROVIDER
from custom_tools.providers.memory_review_inspection import PROVIDER as MEMORY_REVIEW_INSPECTION_PROVIDER
from custom_tools.providers.memory_review_maintenance import PROVIDER as MEMORY_REVIEW_MAINTENANCE_PROVIDER
from custom_tools.providers.objectives import PROVIDER as OBJECTIVES_PROVIDER
from custom_tools.providers.runtime_control import PROVIDER as RUNTIME_CONTROL_PROVIDER
from custom_tools.providers.runtime_settings import PROVIDER as RUNTIME_SETTINGS_PROVIDER
from custom_tools.providers.runtime_settings import MODEL_LOOKUP_PROVIDER, RUNTIME_STATUS_PROVIDER
from custom_tools.providers.schedules import PROVIDER as SCHEDULES_PROVIDER
from custom_tools.providers.schedules import SCHEDULE_RUNS_PROVIDER
from custom_tools.providers.solution_variations import PROVIDER as SOLUTION_VARIATIONS_PROVIDER
from custom_tools.providers.task_queue import PROVIDER as TASK_QUEUE_PROVIDER
from custom_tools.providers.tool_loading import PROVIDER as TOOL_LOADING_PROVIDER
from custom_tools.providers.tool_setup import PROVIDER as TOOL_SETUP_PROVIDER
from custom_tools.providers.worker_pools import PROVIDER as WORKER_POOLS_PROVIDER
from custom_tools.python_env_tool import python_env
from custom_tools.secure_cli_tool import secure_cli
from custom_tools.single_page_site_tools import publish_single_page_site
from custom_tools.tool_result_artifact_tools import inspect_tool_result_artifact
from custom_tools.user_attachment_tools import inspect_user_attachment
from custom_tools.web_research_artifact_tools import (
    brave_llm_context_artifact,
    brave_web_search_artifact,
    web_artifact_inspect,
)
from custom_tools.web_service_tools import capture_web_screenshot, searxng

FILES_PROVIDER = ToolProvider("files", (file_read, file_write, file_move))
CALCULATOR_PROVIDER = ToolProvider("calculator", (calculator,))
LOCAL_CONTEXT_PROVIDER = ToolProvider(
    "local_context",
    (
        inspect_user_attachment,
        load_local_media_for_model_context,
        inspect_tool_result_artifact,
    ),
)

SHELL_EXECUTION_PROVIDER = ToolProvider("execution", (python_env, secure_cli, shell))
PUBLISHING_PROVIDER = ToolProvider("publishing", (publish_single_page_site,))
WEB_RESEARCH_PROVIDER = ToolProvider(
    "web_research",
    (
        exa_search,
        exa_get_contents,
        brave_web_search_artifact,
        brave_llm_context_artifact,
        capture_web_screenshot,
        searxng,
        web_artifact_inspect,
    ),
)


TOOL_PROVIDERS = (
    TASK_QUEUE_PROVIDER,
    MEMORY_REVIEW_INSPECTION_PROVIDER,
    MEMORY_REVIEW_MAINTENANCE_PROVIDER,
    ACCESS_WORKFLOWS_PROVIDER,
    AGENT_MANAGEMENT_PROVIDER,
    EXECUTION_CONTROL_PROVIDER,
    CAPABILITIES_PROVIDER,
    TEAM_SKILL_PLANS_PROVIDER,
    COMMUNICATION_PROVIDER,
    OWN_CONTEXT_PROVIDER,
    CONVERSATIONS_PROVIDER,
    MEMORY_PROVIDER,
    KNOWLEDGE_PROVIDER,
    KNOWLEDGE_CURATION_PROVIDER,
    DIAGNOSTICS_PROVIDER,
    HEARTBEAT_PROVIDER,
    OBJECTIVES_PROVIDER,
    EXECUTION_PLAN_PROVIDER,
    ACCESS_PROVIDER,
    TOOL_LOADING_PROVIDER,
    TOOL_SETUP_PROVIDER,
    CHANGES_PROVIDER,
    ACCESS_PROPOSALS_PROVIDER,
    RUNTIME_CONTROL_PROVIDER,
    RUNTIME_SETTINGS_PROVIDER,
    RUNTIME_STATUS_PROVIDER,
    MODEL_LOOKUP_PROVIDER,
    SCHEDULES_PROVIDER,
    SCHEDULE_RUNS_PROVIDER,
    SOLUTION_VARIATIONS_PROVIDER,
    WORKER_POOLS_PROVIDER,
    FILES_PROVIDER,
    CALCULATOR_PROVIDER,
    LOCAL_CONTEXT_PROVIDER,
    SHELL_EXECUTION_PROVIDER,
    PUBLISHING_PROVIDER,
    WEB_RESEARCH_PROVIDER,
)
ALL_PROVIDER_TOOLS = dedupe_tools(
    tool for provider in TOOL_PROVIDERS for tool in provider.tools
)


def validate_tool_registry() -> None:
    """Reject undeclared or ambiguously owned trusted tools at composition time."""
    providers_by_name = {provider.name: provider for provider in TOOL_PROVIDERS}
    if len(providers_by_name) != len(TOOL_PROVIDERS):
        raise ValueError("Tool provider names must be unique.")
    owned_names = [
        tool_name(tool)
        for provider in TOOL_PROVIDERS
        for tool in provider.tools
    ]
    duplicate_names = sorted(
        name for name in set(owned_names) if owned_names.count(name) > 1
    )
    if duplicate_names:
        raise ValueError(
            "Trusted tools must have one provider owner: " + ", ".join(duplicate_names)
        )
    declared_names = set(owned_names)
    # Every trusted tool needs an access level in the tool catalog
    # (ua_definitions/access/tools/functions.yaml); unclassified tools would
    # otherwise need full access everywhere.
    from access_catalog.catalog import load_catalog

    catalog = load_catalog()
    unclassified = sorted(
        declared_names - set(catalog.functions) - set(catalog.function_dispatchers)
    )
    if unclassified:
        raise ValueError(
            "Trusted tools without an access level in the tool catalog: "
            + ", ".join(unclassified)
        )


validate_tool_registry()
