"""What an agent reports about itself on administrators' Status page.

Each agent adds these findings to the status heartbeat it already publishes
every 20 seconds (``AgentStatus.status_findings``); the gateway keeps the
latest heartbeat per agent and drops agents that stopped reporting, so a
fixed problem disappears with the next heartbeat. Before this, the same facts
were only in logs or in the agent's own diagnostics.
"""

from __future__ import annotations

import logging
from typing import Any

from custom_agents.mcp_server_tools import SERVER_TOOLS_ATTRIBUTE, LoadFailure
from custom_agents.stale_schedules import administrator_findings
from custom_messages.contracts.status_v1 import StatusFindingV1, StatusSeverity

logger = logging.getLogger(__name__)


def agent_status_findings(agent: Any) -> list[StatusFindingV1]:
    """The agent's own findings; a check that fails is logged and left out, never stops the heartbeat."""

    agent_type = str(getattr(agent, "named_agent_type", "") or "agent")
    findings: list[StatusFindingV1] = []
    for check in (_memory_graph, _skill_catalog, _mcp_servers, _stale_schedules, _stopped_receivers):
        try:
            findings.extend(check(agent, agent_type))
        except Exception:  # noqa: BLE001 - a status check never stops the heartbeat that carries it
            logger.exception(
                "An agent status check failed. event=agent_status_check_failed agent_type=%s check=%s",
                agent_type,
                check.__name__,
            )
    return findings


def _stopped_receivers(agent: Any, agent_type: str) -> list[StatusFindingV1]:
    """Receive loops of the agent's NATS adapter that ended without being asked to.

    A loop can no longer end on one message (custom_transport.nats_pull); if it
    ends anyway it is not restarted, so it shows here until the agent restarts.
    """

    from custom_transport.dead_letters import stopped_receiver_finding

    adapter = getattr(getattr(agent, "team_comms", None), "nats_adapter", None)
    if adapter is None:
        return []
    name = str(getattr(agent, "name", "") or getattr(agent, "id", "") or agent_type)
    return [
        stopped_receiver_finding(receiver, source=f"{agent_type} ({name})")
        for receiver in adapter.stopped_receivers()
    ]


def _memory_graph(agent: Any, agent_type: str) -> list[StatusFindingV1]:
    reason = str(getattr(agent, "_memory_graph_degraded_reason", "") or "")
    if not reason:
        return []
    return [
        StatusFindingV1(
            finding_id=f"memory_graph_degraded:{agent_type}",
            severity=StatusSeverity.WARNING,
            title=f"Memory search is not working for {agent_type}",
            summary="The agent works, but cannot search or add to its long-term memory until this is fixed.",
            action="Check the embedding service settings (embedding.*) and its key on /auth model.",
            source=agent_type,
            details=reason,
        )
    ]


def _skill_catalog(agent: Any, agent_type: str) -> list[StatusFindingV1]:
    state = getattr(agent, "_skill_catalog_runtime_state", None)
    error = str(getattr(state, "last_error", "") or "")
    if not error:
        return []
    return [
        StatusFindingV1(
            finding_id=f"skill_catalog_error:{agent_type}",
            severity=StatusSeverity.WARNING,
            title=f"{agent_type} could not refresh its skills",
            summary="The agent keeps the skills it loaded before; newly installed or changed skills are missing.",
            action="Check the skills folder and the team skill changes in the Inbox.",
            source=agent_type,
            details=error,
        )
    ]


def _mcp_servers(agent: Any, agent_type: str) -> list[StatusFindingV1]:
    loader = getattr(agent, SERVER_TOOLS_ATTRIBUTE, None)
    if loader is None:
        return []
    findings = []
    for tool_id, failure in sorted(loader.failures().items()):
        refused = failure.kind is LoadFailure.REFUSED
        findings.append(
            StatusFindingV1(
                finding_id=f"mcp_server_not_loaded:{tool_id}",
                severity=StatusSeverity.WARNING if refused else StatusSeverity.PROBLEM,
                title=f"The MCP server {tool_id} is not available to {agent_type}",
                summary=failure.describe(),
                action=(
                    "Check the access of this agent type to the server in Settings → Access."
                    if refused
                    else "Check that the server is running, then update or reinstall it in Settings → Team → MCP Connections."
                ),
                source=agent_type,
                since=failure.first_failed_at,
                details=failure.error,
            )
        )
    return findings


def _stale_schedules(agent: Any, agent_type: str) -> list[StatusFindingV1]:
    """Schedules the latest maintenance check left for administrators (only the agent that ran it has any)."""

    findings = administrator_findings(str(getattr(agent, "team_id", "") or ""))
    if not findings or not bool(getattr(agent, "is_team_lead", False)):
        return []
    return [
        StatusFindingV1(
            finding_id="schedules_to_check",
            severity=StatusSeverity.WARNING,
            title=f"{len(findings)} schedule(s) need an administrator",
            summary="\n".join(f"{finding.line()} (owner {finding.schedule.owner_id or 'none'})" for finding in findings),
            action="Delete each one with /delete schedule <schedule_id>, or find out why ending its owner's work failed.",
            source=agent_type,
        )
    ]


__all__ = ["agent_status_findings"]
