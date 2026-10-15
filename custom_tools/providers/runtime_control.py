"""Runtime control support for the trusted Strands tool surface."""

import json
import logging

from strands import tool

from custom_messages.contracts.runtime_v1 import RESTART_SCOPE_MANAGER
from custom_agents.runtime_restart_service import RuntimeRestartService
from custom_tools.provider_contract import ToolProvider
from custom_tools.runtime_context import get_agent_ref

logger = logging.getLogger(__name__)


@tool
def plan_runtime_restart(
    reason: str,
    scope: str = RESTART_SCOPE_MANAGER,
) -> str:
    """Stage a supervised restart with an immutable runtime-impact snapshot.

    Records affected processes, active/queued/waiting task identifiers and
    states, and team-visible work for an ``all`` restart. It never restarts
    anything: apply the returned change_id with apply_change; a person
    approves the exact restart (or, with authorization off, agrees in chat).
    """
    agent = get_agent_ref()
    if agent is None:
        return json.dumps({"error": "Agent reference not set"})
    try:
        return json.dumps(
            RuntimeRestartService(agent).stage(scope=scope, reason=reason),
            default=str,
        )
    except Exception as exc:
        return json.dumps({"error": str(exc)}, default=str)


@tool
def get_runtime_restart_status(change_id: str) -> str:
    """Inspect restart plan state, state drift, dispatch receipt, and grant use."""

    agent = get_agent_ref()
    if agent is None:
        return json.dumps({"error": "Agent reference not set"})
    try:
        return json.dumps(
            RuntimeRestartService(agent).status(change_id),
            default=str,
        )
    except Exception as exc:
        return json.dumps({"error": str(exc)}, default=str)


PROVIDER = ToolProvider(
    name="runtime_control",
    tools=(
        plan_runtime_restart,
        get_runtime_restart_status,
    ),
)
