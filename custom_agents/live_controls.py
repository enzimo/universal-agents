"""What an agent answers on its own address about itself: status, pause, resume, exit, a task's state.

These replace the operational subject's agent-to-agent traffic (plan 13B.6, step 7b):
``agent.status`` the status request, ``agent.pause``, ``agent.resume`` and ``agent.exit``
the control instructions, ``task.probe`` the watchdog's task status request, and the team
event ``skills.changed`` the managed skill catalog hint. The host (``custom_agents.agent_host``)
runs each handler on the agent's own loop; the contract's checks (who may send each kind,
signatures, replay) happened before.

- **Status levels.** A basic answer, for any allowed sender (the team lead, the team
  services, the gateway), says what the agent is doing, its queue, its context and budget
  measurements, its skills and its Status page findings. A full answer adds its diagnostics
  and is only for the team lead, checked against the sender's current session (README
  "People, tasks, agent status and steering"). Tools are left out: the contract names tools
  by their catalog ids, which the agent's tool list does not carry.
- **Pause, resume and exit** do what the control instructions did (``UniversalAgent.pause``,
  ``.resume``, ``.exit_agent``); an exiting agent is not paused or resumed. The contract lets
  the team lead's host send them, but that host also runs the lead's thread workers, so
  each request is obeyed only when it comes from the team lead's own session, or from the
  team services for pause and resume (no agent session), checked against the sender's
  current session. An agent never pauses, resumes or stops itself this way, not even the
  lead: stopping the lead's agent would end the manager process without the person's
  approval a restart needs. Anything else is answered ``not_allowed`` and logged
  (``event=agent_control_refused``).
- **Task probes** answer whether the agent holds the task, its state and whether it runs now.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Any, Optional

from custom_messages.enums import ControlType
from custom_messages.wire.encoding import format_timestamp
from custom_messages.wire.frame import VerifiedFrame

_LOGGER = logging.getLogger(__name__)
_DIAGNOSTIC_MAX = 65536
_SKILLS_MAX = 500
_FINDINGS_MAX = 50
_TASK_ID_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9._:@-]{0,127}$"
_PRESSURES = {"low", "moderate", "high", "critical", "unknown"}


def _from_lead(verified: VerifiedFrame) -> bool:
    session = verified.session
    return session is not None and session.agent_role == "lead"


# Who may send pause, resume and exit (module docstring): the team lead's session, and for
# pause and resume also the team services, which send without an agent session.
_TEAM_SERVICES_ROLE = "team_services"
_SERVICES_MAY_SEND = frozenset({"pause", "resume"})


def _control_refusal(agent: Any, verified: VerifiedFrame, action: str) -> Optional[dict[str, Any]]:
    """The ``not_allowed`` answer when this control must not be obeyed (module docstring); None to obey it."""

    session = verified.session
    certificate = verified.certificate
    if session is None:
        if action in _SERVICES_MAY_SEND and certificate is not None and _TEAM_SERVICES_ROLE in certificate.roles:
            return None
        why = "Only the team lead or the team services may ask this."
    elif str(session.agent) == str(agent.id):
        why = "An agent does not pause, resume or stop itself through a control request."
    elif session.agent_role == "lead":
        return None
    else:
        why = "Only the team lead may ask this."
    issuer = verified.frame.issuer
    agent._logger.warning("[%s] An agent control was refused. event=agent_control_refused agent_id=%s action=%s "
                          "from_runtime=%s from_agent=%s from_role=%s", agent.id, agent.id, action, issuer.runtime,
                          getattr(issuer, "agent", None), getattr(session, "agent_role", None))
    return {"outcome": "not_allowed", "message": why}


def _per_mille(ratio: Any) -> Optional[int]:
    if not isinstance(ratio, (int, float)):
        return None
    return max(0, min(1_000_000, round(float(ratio) * 1000)))


def _timestamp(value: Any) -> Optional[str]:
    if not value:
        return None
    moment = value if isinstance(value, datetime) else datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    return format_timestamp(moment if moment.tzinfo else moment.replace(tzinfo=timezone.utc))


def status_reply(agent: Any, *, full: bool) -> dict[str, Any]:
    """The ``agent.status`` answer from the agent's own status (module docstring)."""

    import re

    status = agent.get_self_status(info_type=ControlType.STATUS_REQUEST_FULL if full else
                                   ControlType.STATUS_REQUEST_BASIC)
    reply: dict[str, Any] = {
        "outcome": "ok", "agent_type": str(status.named_agent_type), "is_lead": bool(status.is_team_lead),
        "paused": bool(status.is_paused), "executing": bool(status.is_executing),
        "has_capacity": bool(status.has_execution_capacity), "accepting_work": bool(status.accepting_work),
        "exiting": bool(status.is_exiting), "queued": int(status.top_level_queue_depth or 0),
        "waiting": int(status.waiting_top_level_task_count or 0), "deferred": int(status.deferred_message_count or 0),
    }
    if status.current_task_id and re.match(_TASK_ID_PATTERN, str(status.current_task_id)):
        reply["current_task_id"] = str(status.current_task_id)
        if status.current_task_age_seconds is not None:
            reply["current_task_age_seconds"] = max(0, int(status.current_task_age_seconds))
    if status.context_window_tokens and status.estimated_context_tokens is not None:
        pressure = str(status.context_pressure_level or "unknown")
        reply["context"] = {"window_tokens": int(status.context_window_tokens),
                            "estimated_tokens": int(status.estimated_context_tokens),
                            "fill_per_mille": _per_mille(status.context_fill_ratio) or 0,
                            "pressure": pressure if pressure in _PRESSURES else "unknown"}
    for field, ratio in (("tool_call_budget_per_mille", status.tool_call_budget_fill_ratio),
                         ("tool_result_budget_per_mille", status.tool_result_budget_fill_ratio)):
        value = _per_mille(ratio)
        if value is not None:
            reply[field] = value
    skills = [str(name)[:160] for name in (status.available_skills or []) if str(name)][:_SKILLS_MAX]
    if skills:
        reply["skills"] = skills
    findings = []
    for finding in list(status.status_findings or [])[:_FINDINGS_MAX]:
        item: dict[str, Any] = {"finding_id": finding.finding_id, "severity": finding.severity.value,
                                "title": finding.title}
        for name in ("summary", "action", "source", "details"):
            value = getattr(finding, name, "")
            if value:
                item[name] = value
        since = _timestamp(finding.since)
        if since:
            item["since"] = since
        findings.append(item)
    if findings:
        reply["findings"] = findings
    if full and status.diagnostic:
        diagnostic = json.dumps(status.diagnostic, default=str, sort_keys=True)
        if len(diagnostic) > _DIAGNOSTIC_MAX:
            diagnostic = json.dumps({"truncated": True, "keys": sorted(status.diagnostic)[:200]})[:_DIAGNOSTIC_MAX]
        reply["diagnostic_json"] = diagnostic
    return reply


async def agent_status(agent: Any, verified: VerifiedFrame) -> dict[str, Any]:
    full = verified.raw["body"]["level"] == "full"
    if full and not _from_lead(verified):
        return {"outcome": "not_allowed", "message": "Only the team lead reads an agent's full status."}
    from custom_agents.skill_catalog_runtime import repair_agent_skill_catalog

    # As the status request did: a skill change this agent missed is picked up before it answers.
    await repair_agent_skill_catalog(agent, reason="agent_status_request")
    return status_reply(agent, full=full)


def _exiting(agent: Any) -> bool:
    should_exit = getattr(agent, "_should_exit", None)
    return bool(should_exit is not None and should_exit.is_set())


async def agent_pause(agent: Any, verified: VerifiedFrame) -> dict[str, Any]:
    refused = _control_refusal(agent, verified, "pause")
    if refused is not None:
        return refused
    if _exiting(agent):
        return {"outcome": "agent_exiting", "message": "The agent is exiting."}
    if agent.paused:
        return {"outcome": "ok", "state": "already_paused"}
    _log_control(agent, verified, "pause")
    await agent.pause()
    return {"outcome": "ok", "state": "paused"}


async def agent_resume(agent: Any, verified: VerifiedFrame) -> dict[str, Any]:
    refused = _control_refusal(agent, verified, "resume")
    if refused is not None:
        return refused
    if _exiting(agent):
        return {"outcome": "agent_exiting", "message": "The agent is exiting."}
    if not agent.paused:
        return {"outcome": "ok", "state": "not_paused"}
    _log_control(agent, verified, "resume")
    await agent.resume()
    return {"outcome": "ok", "state": "resumed"}


async def agent_exit(agent: Any, verified: VerifiedFrame) -> dict[str, Any]:
    refused = _control_refusal(agent, verified, "exit")
    if refused is not None:
        return refused
    if _exiting(agent):
        return {"outcome": "ok", "state": "already_exiting"}
    _log_control(agent, verified, "exit")
    await agent.exit_agent()
    return {"outcome": "ok", "state": "exiting"}


def _log_control(agent: Any, verified: VerifiedFrame, action: str) -> None:
    issuer = verified.frame.issuer
    reason = verified.raw["body"].get("reason")
    agent._logger.info("[%s] An agent control arrived. event=agent_control_received action=%s from_runtime=%s "
                       "from_agent=%s reason=%s", agent.id, action, issuer.runtime, getattr(issuer, "agent", None),
                       reason)


async def task_probe(agent: Any, verified: VerifiedFrame) -> dict[str, Any]:
    """Whether this agent holds the task, its state, and whether it runs now (the watchdog's question)."""

    from custom_agents.turn_registry import executing_task_ids

    task_id = str(verified.raw["body"]["task_id"])
    task_manager = agent.task_manager
    task_info = await task_manager.operations_runtime.run(task_manager.get_task_info, task_id)
    reply: dict[str, Any] = {"outcome": "ok", "known": task_info is not None,
                             "current": task_info is not None and task_id in executing_task_ids(agent),
                             "executing": bool(agent.is_executing), "exiting": _exiting(agent)}
    if task_info is not None:
        state = getattr(task_info.state, "value", task_info.state)
        if state is not None:
            reply["state"] = str(state)
        if task_info.task.task_state:
            updated = _timestamp(task_info.task.task_state[-1][1])
            if updated:
                reply["last_update_at"] = updated
    return reply


async def skills_changed(agent: Any, verified: VerifiedFrame) -> dict[str, Any]:
    """The team lead installed or removed a managed skill: refresh from the verified receipt.

    Only the team lead's own session sends it; other agents of the lead's process may not.
    """

    from custom_agents.skill_catalog_runtime import apply_managed_skill_catalog_change
    from custom_messages.skill_catalog import ManagedSkillCatalogChangedV1

    body = verified.raw["body"]
    if not _from_lead(verified) or verified.session is None:
        agent._logger.warning("[%s] A skill change event not sent by the team lead was ignored. "
                              "event=skills_changed_sender_rejected change_id=%s", agent.id, body.get("change_id"))
        return {}
    notification = ManagedSkillCatalogChangedV1(
        team_id=str(verified.session.team), source_agent_id=str(verified.session.agent),
        operation=body["operation"], change_id=body["change_id"], skill_name=body["skill_name"],
        installed_tree_sha256=body["installed_tree_sha256"],
    )
    await apply_managed_skill_catalog_change(agent, notification)
    return {}


LIVE_CONTROL_HANDLERS = {
    "agent.status": agent_status,
    "agent.pause": agent_pause,
    "agent.resume": agent_resume,
    "agent.exit": agent_exit,
    "task.probe": task_probe,
}
EVENT_HANDLERS = {"skills.changed": skills_changed}
