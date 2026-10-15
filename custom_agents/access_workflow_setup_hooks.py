"""Hooks that keep access workflow steps current with tool setup pages.

The setup run runtime calls ``link_setup_request`` after it sends a
``ToolSetupRequestV1`` (``custom_agents.tool_setup_run_runtime``), and the setup result handler calls
``record_setup_result`` when the gateway's ``ToolSetupResultV1`` arrives
(``custom_agents.access_workflow_setups`` has the rules). The person comes only
from the task's verified signed task context (``resolve_meta_identity`` on
the task's meta). Callers run them off the event loop (``asyncio.to_thread``):
they read and write the workflow store. The hooks never raise: a failure is
logged and the page shows the setup as it was until the next result.

They live apart from ``access_workflow_setups`` because they need the runtime's
identity checks (``custom_agents.access_runtime``), which import the workflow
service; keeping them here keeps the import graph acyclic.
"""

from __future__ import annotations

import logging
from typing import Any

from custom_agents.access_runtime import resolve_meta_identity
from custom_agents.access_workflow_setups import WorkflowSetups
from custom_agents.access_workflows import workflow_service_for
from custom_messages.contracts.credentials_v1 import ToolSetupRequestV1, ToolSetupResultV1

logger = logging.getLogger(__name__)


def link_setup_request(agent: Any, request: ToolSetupRequestV1, meta: Any) -> None:
    """Link a setup page trusted code just sent to its workflow step (never raises)."""

    def link(setups: WorkflowSetups, identity: Any) -> None:
        setups.link(
            request,
            team_id=identity.team_id,
            person_id=identity.person_id,
            context_id=identity.context_id,
            step_title=_step_title(agent, request),
        )

    _safely(agent, "link", request.setup_request_id, link, meta=meta)


def _step_title(agent: Any, request: ToolSetupRequestV1) -> str:
    """An external step's approved title from the catalog (empty for other pages)."""

    if not request.step_id:
        return ""
    from custom_agents.access_runtime import runtime_access

    tool = runtime_access(agent).catalog.tools.get(request.tool_id)
    step = tool.setup.step(request.step_id) if tool is not None and tool.setup else None
    return step.title if step else ""


def record_setup_result(agent: Any, result: ToolSetupResultV1, meta: Any) -> None:
    """The result handler's hook: update the step a setup page is linked to (never raises)."""

    _safely(agent, "result", result.setup_request_id, lambda setups, identity: setups.record_result(
        result, team_id=identity.team_id, person_id=identity.person_id, context_id=identity.context_id
    ), meta=meta)


def _safely(agent: Any, what: str, setup_request_id: str, action: Any, *, meta: Any) -> None:
    """Workflow bookkeeping must never stop a setup request or its result (logged)."""

    try:
        identity = resolve_meta_identity(agent, meta)
        if identity is None or identity.person_id is None:
            return
        action(WorkflowSetups(workflow_service_for(agent)), identity)
    except Exception as exc:  # noqa: BLE001 - the page shows the setup as waiting until the next result
        logger.warning(
            "Could not update a workflow step for a setup page. event=workflow_setup_update_failed what=%s "
            "setup_request_id=%s error=%s",
            what,
            setup_request_id,
            type(exc).__name__,
            exc_info=exc,
        )


__all__ = ["link_setup_request", "record_setup_result"]
