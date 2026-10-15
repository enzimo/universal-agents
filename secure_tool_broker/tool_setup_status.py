"""Whether tools are set up for the person a task works for: the broker's side of ``set_up_tools``.

TeamLead checks, before it acts, whether a tool that uses an account can work
for the person (owner's decision 2026-09-28). The agent side
(``custom_tools.providers.tool_setup``) decides access; this broker action
(``tool_setup.status``, ``procedure:tool_setup`` in the tool catalog) reports
what only the broker knows, per tool:

- ``sign_in``: for tools a person signs in to (``secure_tools.auth_methods``),
  whether the person signed in: ``signed_in`` (with the account name when
  the broker knows it), ``missing``, or ``unknown`` when the check failed;
- ``link``: for tools that use a linked credential, whether the person's own
  account is linked, and whether a team account is linked that serves them
  (only while the team shares the service, ``access_control.account_sharing``);
- ``team_sign_in_app``: for Google and X, whether the sign-in app people sign
  in through is set up (``configured``, ``missing``, ``unknown``;
  ``not_needed`` for every other tool). A person's own Google OAuth client
  counts as set up for them.

The request passed the access gate like every agent broker action, so the
person is the verified person of the task's signed context; nothing here
reads a person from request fields. The answer carries states and account
names only: never tokens, client secrets, credential keys or page addresses.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Mapping
from typing import Any

from access_control.account_sharing import TeamAccountSharing
from custom_messages.contracts.access_v2 import AccountMode, LinkOwnerKind, ToolDefinitionV1
from secure_tool_broker.credential_state import credential_owner_context
from secure_tools.auth_methods import X_SIGN_IN_PROVIDER, ToolSignIn, tool_sign_in
from secure_tools.providers import ToolContext
from access_control.tool_setup_states import SOURCE_SETUP_CHECK, ToolSetupStates, safely, verdict_from_status

LOGGER = logging.getLogger(__name__)

TOOL_SETUP_PROVIDER = "tool_setup"
MAX_TOOLS = 12
SIGNED_IN = "signed_in"
MISSING = "missing"
UNKNOWN = "unknown"
CONFIGURED = "configured"
NOT_NEEDED = "not_needed"
_SIGNED_IN_STATUSES = frozenset({"authenticated", SIGNED_IN})
_ACCOUNT_LABEL_MAX = 120
# gh auth status: "Logged in to github.com account exampleuser (keyring)".
_GH_ACCOUNT = re.compile(r"account\s+([A-Za-z0-9-]{1,39})\b")


def _tool_ids(params: Mapping[str, Any]) -> list[str]:
    raw = params.get("tool_ids")
    if not isinstance(raw, (list, tuple)) or not raw:
        raise ValueError("params.tool_ids must list the tool ids to check.")
    ids = list(dict.fromkeys(str(item or "").strip() for item in raw if str(item or "").strip()))
    return ids[:MAX_TOOLS]


def _account_label(provider: str, details: Mapping[str, Any]) -> str | None:
    """The account the person signed in with, from the status check, or None."""

    if provider == "github":
        text = f"{details.get('stdout') or ''}\n{details.get('stderr') or ''}"
        match = _GH_ACCOUNT.search(text)
        label = match.group(1) if match else ""
    else:
        label = str(details.get("account") or "")
    label = " ".join(label.split())[:_ACCOUNT_LABEL_MAX]
    return label or None


def _sign_in_state(sign_in: ToolSignIn, person_id: str, team_id: str) -> dict[str, Any]:
    from secure_tool_broker.executors import get_auth_status

    try:
        with credential_owner_context(person_id):
            status = get_auth_status(sign_in.provider, team_id=team_id)
    except Exception as exc:  # noqa: BLE001 - reported as unknown; the check never blocks the answer
        LOGGER.warning(
            "A sign-in status check failed. event=tool_setup_sign_in_check_failed provider=%s error=%s",
            sign_in.provider,
            type(exc).__name__,
        )
        return {"state": UNKNOWN, "note": "The sign-in could not be checked just now."}
    if status.authenticated or status.status in _SIGNED_IN_STATUSES:
        return {"state": SIGNED_IN, "account": _account_label(sign_in.provider, status.details or {})}
    if status.status == "auth_required":
        return {"state": MISSING}
    return {"state": UNKNOWN, "note": " ".join(str(status.message or "").split())[:300]}


def _team_app_state(sign_in: ToolSignIn | None, person_id: str, team_id: str) -> str:
    if sign_in is None or sign_in.team_client_command is None:
        return NOT_NEEDED
    try:
        if sign_in.provider == X_SIGN_IN_PROVIDER:
            from secure_tool_broker.x_sign_in import team_client

            return CONFIGURED if team_client(team_id) is not None else MISSING
        from secure_tool_broker.google_workspace import oauth_client_for

        return CONFIGURED if oauth_client_for(person_id, team_id) is not None else MISSING
    except Exception as exc:  # noqa: BLE001 - reported as unknown
        LOGGER.warning(
            "The team's sign-in app could not be checked. event=tool_setup_team_app_check_failed "
            "provider=%s error=%s",
            sign_in.provider,
            type(exc).__name__,
        )
        return UNKNOWN


def _link_state(gate: Any, tool: ToolDefinitionV1, person_id: str) -> dict[str, bool]:
    links = gate.links
    if links is None:
        return {"own": False, "team": False}
    team_id = gate.team_id
    team_used = TeamAccountSharing(links, team_id).team_account_used(tool)
    return {
        "own": links.active_link(team_id, tool.tool_id, LinkOwnerKind.PERSON, person_id) is not None,
        "team": team_used and links.active_link(team_id, tool.tool_id, LinkOwnerKind.TEAM, team_id) is not None,
    }


def tool_status_entry(tool_id: str, person_id: str, gate: Any) -> dict[str, Any]:
    tool = gate.catalog.tools.get(tool_id)
    if tool is None:
        return {"tool_id": tool_id, "known": False}
    sign_in = tool_sign_in(tool_id)
    entry: dict[str, Any] = {
        "tool_id": tool_id,
        "known": True,
        "name": tool.display_name,
        "account_mode": tool.account.value,
        "team_sign_in_app": _team_app_state(sign_in, person_id, gate.team_id),
    }
    if sign_in is not None:
        entry["sign_in"] = _sign_in_state(sign_in, person_id, gate.team_id)
    if tool.account is AccountMode.LINKED_CREDENTIAL:
        entry["link"] = _link_state(gate, tool, person_id)
    return entry


def tool_setup_status(params: Mapping[str, Any], context: ToolContext, gate: Any) -> dict[str, Any]:
    """The broker response for ``tool_setup.status`` (after the access gate allowed it)."""

    person_id = str(context.canonical_user_id or "").strip()
    if not person_id:
        return {"status": "error", "error": "Tool setup is checked for a person; this task works for nobody."}
    try:
        tool_ids = _tool_ids(params)
    except ValueError as exc:
        return {"status": "error", "error": str(exc)}
    tools = [tool_status_entry(tool_id, person_id, gate) for tool_id in tool_ids]
    for entry in tools:
        record_status(gate, person_id, entry, source=SOURCE_SETUP_CHECK)
    LOGGER.info(
        "Checked tool setup for a task. event=tool_setup_status_checked task_id=%s agent_id=%s tools=%s",
        context.task_id,
        context.agent_id,
        ",".join(tool_ids),
    )
    return {"status": "success", "content": {"tools": tools}}


def record_status(gate: Any, person_id: str, entry: dict[str, Any], *, source: str) -> Any:
    """Record the state one status entry shows, and add the stored state to the entry.

    Returns the verdict (``verdict_from_status``), or None for a tool that
    acts with no account or is not in the catalog.
    """

    verdict = verdict_from_status(entry)
    if verdict is None or gate.links is None:
        return verdict
    states = ToolSetupStates(gate.links)
    safely(
        lambda: states.note_verdict(gate.team_id, person_id, entry["tool_id"], verdict, source=source),
        what="setup_status",
        tool_id=entry["tool_id"],
        person_id=person_id,
    )
    stored = states.get(gate.team_id, person_id, entry["tool_id"])
    if stored is not None:
        entry["known_state"] = stored.model_dump(
            mode="json", include={"state", "how", "account_label", "missing_step", "fixed_by", "last_worked_at_utc"}
        )
    return verdict


__all__ = ["MAX_TOOLS", "TOOL_SETUP_PROVIDER", "record_status", "tool_setup_status", "tool_status_entry"]
