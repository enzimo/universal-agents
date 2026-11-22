"""Keep each person's tool setup state from what the broker sees (``access_control.tool_setup_states``).

The broker is where tools that act with an account succeed or fail, so it
records the outcome of every call of one for the person the task works for:

- a successful action (``/v1/tools/<provider>``) or MCP call means ``ready``,
  with how the broker chose the account (the person's sign-in, their own or
  the team's linked account); a web request counts only when the service
  answered with 2xx or 3xx, and a 401 means the linked account was refused;
- a failure whose error code is about setup (``SETUP_FAILURE_CODES``) means
  ``needs_setup`` with the step, and an account link the access gate found
  missing means ``needs_setup`` for an account link;
- anything else (a network error, 5xx, a rate limit, a refused request)
  changes nothing.

Only tools that act with an account are tracked, and only work for a person.
Recording never fails the call it describes.
"""

from __future__ import annotations

from typing import Any

from access_control.tool_setup_states import (
    HOW_GATE_LINK,
    HOW_OWN_LINK,
    HOW_SIGNED_IN,
    HOW_TEAM_LINK,
    SOURCE_TOOL_CALL,
    STEP_ACCOUNT_LINK,
    STEP_GATE_LINK,
    ToolSetupStates,
    safely,
    step_for_failure,
)
from custom_messages.contracts.access_v2 import (
    REASON_ACCOUNT_LINK_MISSING,
    REASON_GATE_LINK_MISSING,
    AccountLinkV1,
    AccountMode,
    LinkOwnerKind,
    ToolDefinitionV1,
)
from secure_tools.auth_methods import tool_sign_in
from secure_tools.providers import ToolContext, ToolResult

UNAUTHORIZED = 401


def how_account_was_chosen(
    tool_id: str, link: AccountLinkV1 | None, *, gate_link: AccountLinkV1 | None = None
) -> str:
    """How the broker chose the credential for a call that worked."""

    if link is None:
        if tool_sign_in(tool_id) is not None:
            return HOW_SIGNED_IN
        return HOW_GATE_LINK if gate_link is not None else HOW_TEAM_LINK
    return HOW_OWN_LINK if link.owner_kind is LinkOwnerKind.PERSON else HOW_TEAM_LINK


def _account_tool(gate: Any, provider: str, action: str, params: Any) -> ToolDefinitionV1 | None:
    """The catalog tool an action used when it acts with an account or team gate, else None."""

    tool_id = gate.catalog.resolve_broker_action(provider, action, params).tool_id
    tool = gate.catalog.tools.get(tool_id)
    if tool is None:
        return None
    return tool if tool.account is not AccountMode.NONE or bool(tool.gate_injections) else None


def _outcome(result: ToolResult, *, gate_only: bool = False) -> tuple[str, str | None]:
    """``("worked", None)``, ``("needs_setup", error_code)`` or ``("ignore", None)`` for one result."""

    content = dict(result.content or {})
    if result.status != "success":
        return "needs_setup", str(content.get("error_code") or "")
    status_code = content.get("http_status")
    if status_code is None or 200 <= int(status_code) < 400:
        return "worked", None
    if int(status_code) == UNAUTHORIZED:
        return "needs_setup", REASON_GATE_LINK_MISSING if gate_only else REASON_ACCOUNT_LINK_MISSING
    return "ignore", None


def note_broker_result(gate: Any, validated: Any, context: ToolContext, result: ToolResult) -> None:
    """Record the setup state one broker action's result shows (module docstring)."""

    person_id = str(context.canonical_user_id or "")
    if not person_id or gate.links is None:
        return

    def record() -> None:
        tool = _account_tool(gate, validated.provider, validated.action, validated.params)
        if tool is None:
            return
        gate_only = tool.account is AccountMode.NONE and bool(tool.gate_injections)
        outcome, code = _outcome(result, gate_only=gate_only)
        states = ToolSetupStates(gate.links)
        if outcome == "worked":
            how = how_account_was_chosen(tool.tool_id, context.account_link, gate_link=context.gate_link)
            states.note_ready(gate.team_id, person_id, tool.tool_id, how=how, source=SOURCE_TOOL_CALL)
            return
        step = step_for_failure(tool.tool_id, code or "") if outcome == "needs_setup" else None
        if step is not None:
            states.note_needs_setup(gate.team_id, person_id, tool.tool_id, step=step, source=SOURCE_TOOL_CALL)

    safely(record, what="broker_action", provider=validated.provider, action=validated.action, person_id=person_id)


def note_link_missing(links: Any, team_id: str, person_id: str | None, tool_id: str) -> None:
    """The access gate found no usable account linked for the tool (missing or outdated)."""

    if links is None or not person_id:
        return
    safely(
        lambda: ToolSetupStates(links).note_needs_setup(
            team_id, person_id, tool_id, step=STEP_ACCOUNT_LINK, source=SOURCE_TOOL_CALL
        ),
        what="account_link_missing",
        tool_id=tool_id,
        person_id=person_id,
    )


def note_gate_link_missing(links: Any, team_id: str, person_id: str | None, tool_id: str) -> None:
    """The access gate found no usable team gate credential linked for the tool (missing or outdated)."""

    if links is None or not person_id:
        return
    safely(
        lambda: ToolSetupStates(links).note_needs_setup(
            team_id, person_id, tool_id, step=STEP_GATE_LINK, source=SOURCE_TOOL_CALL
        ),
        what="gate_link_missing",
        tool_id=tool_id,
        person_id=person_id,
    )


def note_mcp_worked(gate: Any, tool_id: str, person_id: str | None, link: AccountLinkV1 | None) -> None:
    """An MCP call that used a linked account worked."""

    if link is None or gate.links is None or not person_id:
        return
    safely(
        lambda: ToolSetupStates(gate.links).note_ready(
            gate.team_id, person_id, tool_id, how=how_account_was_chosen(tool_id, link), source=SOURCE_TOOL_CALL
        ),
        what="mcp_call",
        tool_id=tool_id,
        person_id=person_id,
    )


__all__ = [
    "how_account_was_chosen",
    "note_broker_result",
    "note_gate_link_missing",
    "note_link_missing",
    "note_mcp_worked",
]
