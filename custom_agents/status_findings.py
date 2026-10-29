"""What an agent reports about itself on administrators' Status page.

Each agent adds these findings to the status heartbeat it already publishes
every 20 seconds (``AgentStatus.status_findings``); the gateway keeps the
latest heartbeat per agent and drops agents that stopped reporting, so a
fixed problem disappears with the next heartbeat. Before this, the same facts
were only in logs or in the agent's own diagnostics.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

from custom_agents.mcp_server_tools import LoadFailure, server_loader
from custom_agents.stale_schedules import administrator_findings
from custom_messages.contracts.status_v1 import StatusFindingV1, StatusSeverity

logger = logging.getLogger(__name__)


def agent_status_findings(agent: Any) -> list[StatusFindingV1]:
    """The agent's own findings; a check that fails is logged and left out, never stops the heartbeat."""

    agent_type = str(getattr(agent, "named_agent_type", "") or "agent")
    findings: list[StatusFindingV1] = []
    for check in (
        _memory_graph,
        _skill_catalog,
        _mcp_servers,
        _stale_schedules,
        _stopped_receivers,
        _output_retention_overdue,
        _output_retention_grace,
    ):
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
    loader = server_loader(agent)
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


def _output_retention_overdue(agent: Any, agent_type: str) -> list[StatusFindingV1]:
    """The saved tool output cleanup has not succeeded for two days (only the team lead runs it).

    It runs before each internal-improvement maintenance run and records
    each run (``custom_agents.output_retention.write_run_record``); a schedule
    that is off or paused, or a cleanup that keeps failing, shows here.
    """

    from custom_agents.output_retention import RETENTION_SETTING, overdue_since, read_run_record, retention_days_of
    from custom_tools.workspace_scope import current_instance_layout

    if not bool(getattr(agent, "is_team_lead", False)):
        return []
    days = retention_days_of(agent)
    layout = current_instance_layout(agent)
    if days <= 0 or layout is None:
        return []
    record = read_run_record(layout.output_retention_record_path)
    since = overdue_since(record)
    if since is None:
        return []
    when = datetime.fromtimestamp(since, timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    last = f"last succeeded at {when}" if record.get("last_success_at") else f"has not succeeded since {when}"
    return [
        StatusFindingV1(
            finding_id="output_retention_overdue",
            severity=StatusSeverity.WARNING,
            title="Old saved tool outputs are not being deleted",
            summary=(
                f"The cleanup that deletes saved tool outputs older than {days} days ({RETENTION_SETTING}) "
                f"runs before each internal-improvement maintenance run; it {last}."
            ),
            action=(
                "Check that the internal-improvement maintenance schedule is on and not paused "
                "(agent.schedule_maintenance.internal_improvement_review_interval_seconds above 0), and look for "
                "event=output_retention_failed in the manager log."
            ),
            source=agent_type,
        )
    ]


def _output_retention_grace(agent: Any, agent_type: str) -> list[StatusFindingV1]:
    """During the grace period: when deletion starts and what the latest dry run found.

    Shown while the latest run was a dry run; it clears with the first run
    that deletes (``custom_agents.output_retention.remove_old_outputs_and_record``).
    """

    from custom_agents.output_retention import read_run_record, retention_days_of, size_text
    from custom_tools.workspace_scope import current_instance_layout

    if not bool(getattr(agent, "is_team_lead", False)) or retention_days_of(agent) <= 0:
        return []
    layout = current_instance_layout(agent)
    record = read_run_record(layout.output_retention_record_path) if layout is not None else {}
    if not record.get("dry_run") or not record.get("deletes_from"):
        return []
    deletes_from = datetime.fromisoformat(str(record["deletes_from"])).strftime("%Y-%m-%d %H:%M UTC")
    kinds = record.get("kinds") or {}
    items = sum(int(kind.get("items") or 0) for kind in kinds.values())
    size = sum(int(kind.get("bytes") or 0) for kind in kinds.values())
    found = "; ".join(
        f"{name.replace('_', ' ')}: {kind.get('items', 0)} items, {kind.get('files', 0)} files, "
        f"{size_text(int(kind.get('bytes') or 0))}"
        for name, kind in sorted(kinds.items())
    )
    return [
        StatusFindingV1(
            finding_id="output_retention_grace_period",
            severity=StatusSeverity.NOTE,
            title=f"Old saved tool outputs will be deleted from {deletes_from}",
            summary=(
                f"Saved tool outputs older than {record.get('retention_days')} days: about {items} items "
                f"({size_text(size)}) at the latest maintenance run ({found}). Nothing is deleted before that date; "
                "chat transcripts, task links, summaries and completed tasks are always kept."
            ),
            action=(
                "Keep the default, change the number of days in Settings → Team → File Retention, or set it to 0 "
                "to keep everything. A change applies after the next restart."
            ),
            source=agent_type,
        )
    ]


__all__ = ["agent_status_findings"]
