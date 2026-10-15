"""Setup page results and TeamLead's line of known tool setup states.

Setup pages are opened only by trusted code for a ``set_up_tools`` run
(owner's decision 2026-09-28; ``custom_agents.tool_setup_run_runtime``); the
model never writes a setup request. The gateway sends one
``ToolSetupResultV1`` per page, and this module hands it to the run that
waits for it, which records it, offers an expired page again, and resumes the
task once everything is done. A result whose task is gone closes the
requester's wait with a plain answer instead of reviving the task; a result
no run waits for (a page from before the run was stored, or one the run
already stopped waiting on) is logged and ignored.

``tool_setup_context`` writes TeamLead's one pinned context line with the
person's last known setup states, so a ready tool is used without setting it
up again.
"""

from __future__ import annotations

from typing import Any

from custom_agents.task_message_runtime import (
    _ORPHANED_CREDENTIAL_WORKFLOW_ERROR,
    _publish_task_response,
)
from custom_messages.contracts.credentials_v1 import (
    TOOL_SETUP_COMPLETED,
    TOOL_SETUP_DECLINED,
    TOOL_SETUP_EXPIRED,
    ToolSetupResultV1,
)
from custom_messages.contracts.routing_v1 import MessageHeader, MessageMeta
from custom_messages.contracts.task_v1 import Task
from custom_messages.enums import MessageMetaType

_WHAT_BY_KIND = {
    "sign_in": "the sign-in",
    "account_link": "the account link",
    "team_sign_in_app": "the team's sign-in app",
    "external_step": "the setup step",
}


def orphaned_tool_setup_answer(result: ToolSetupResultV1) -> str:
    """A plain answer when the task that asked is no longer active."""

    what = _WHAT_BY_KIND.get(result.kind, "the setup")
    if result.status == TOOL_SETUP_COMPLETED:
        return f"{what[0].upper()}{what[1:]} for {result.tool_id} is done. Send a new message to continue."
    if result.status in {TOOL_SETUP_DECLINED, TOOL_SETUP_EXPIRED}:
        return f"{what[0].upper()}{what[1:]} for {result.tool_id} was not finished. Nothing was changed."
    return f"I couldn't set up {result.tool_id}. Nothing was changed."


async def handle_tool_setup_result(
    self: Any,
    setup_result: ToolSetupResultV1,
    incoming_msg_header: MessageHeader,
    incoming_msg_meta: MessageMeta,
) -> None:
    """Hand a page's result to the run that waits for it (module docstring)."""

    from custom_agents.tool_setup_run_runtime import on_setup_result

    run = self.task_manager.operations_runtime.run
    parent_task_id = setup_result.parent_task_id
    parent_task_info = await run(self.task_manager.get_task_info, parent_task_id)
    if parent_task_info is None or await run(self.task_manager.is_terminal_state, parent_task_info.state):
        self._logger.warning(
            "[%s] The task a tool setup result names is not active; closing the requester's wait "
            "without reviving it. event=tool_setup_result_orphaned task_id=%s setup_request_id=%s "
            "tool_id=%s kind=%s status=%s",
            self.id,
            parent_task_id,
            setup_result.setup_request_id,
            setup_result.tool_id,
            setup_result.kind,
            setup_result.status,
        )
        await _publish_task_response(
            self,
            response_task=Task(
                task_id=parent_task_id,
                task_type=MessageMetaType.RESPONSE,
                answer=orphaned_tool_setup_answer(setup_result),
                errors=[setup_result.message or _ORPHANED_CREDENTIAL_WORKFLOW_ERROR],
            ),
            incoming_header=incoming_msg_header,
            incoming_meta=incoming_msg_meta,
        )
        return
    if await on_setup_result(self, setup_result, incoming_msg_header, incoming_msg_meta):
        return
    self._logger.warning(
        "[%s] No setup run waits for this page's result; it is ignored. event=tool_setup_result_unmatched "
        "task_id=%s setup_request_id=%s tool_id=%s kind=%s status=%s",
        self.id,
        parent_task_id,
        setup_result.setup_request_id,
        setup_result.tool_id,
        setup_result.kind,
        setup_result.status,
    )


def tool_setup_context(agent: Any, meta: Any) -> str:
    """TeamLead's one runtime-written line of the person's known tool setup states.

    Only the verified person of the task (its signed context) and only that
    person's rows, so one person's accounts never show in another's work.
    Empty for other agents, for work for nobody, and without states.
    """

    from access_control.tool_setup_states import ToolSetupStates, describe_states
    from custom_agents.access_runtime import resolve_meta_identity, runtime_access
    from secure_tools.auth_methods import tool_sign_in
    from storage.access_repository import AccessRepository

    if not getattr(agent, "is_team_lead", False):
        return ""
    identity = resolve_meta_identity(agent, meta)
    if identity is None or identity.person_id is None:
        return ""
    try:
        states = ToolSetupStates(AccessRepository(agent.task_manager.storage_settings)).for_person(
            identity.team_id, identity.person_id
        )
        catalog = runtime_access(agent).catalog
    except Exception as exc:  # noqa: BLE001 - the line is a convenience; the turn goes on without it
        agent._logger.warning(
            "Could not read tool setup states for TeamLead's context. event=tool_setup_context_failed error=%s",
            type(exc).__name__,
        )
        return ""
    names: dict[str, str] = {}
    for state in states:
        sign_in = tool_sign_in(state.tool_id)
        tool = catalog.tools.get(state.tool_id)
        names[state.tool_id] = sign_in.display_name if sign_in else (tool.display_name if tool else state.tool_id)
    return describe_states(states, names)


__all__ = [
    "handle_tool_setup_result",
    "orphaned_tool_setup_answer",
    "tool_setup_context",
]
