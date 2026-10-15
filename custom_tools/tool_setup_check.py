"""The setup check behind ``set_up_tools``: is everything set up for the tools a request needs?

For the person the task works for, per tool (owner's decision 2026-09-28):

- access: may TeamLead and the person use the tool at the level the work
  needs, and, when a named worker type does the work, may TeamLead hand it on
  to the worker's task without asking (authorization on; the access
  service's side-effect-free ``holds`` and ``may_give`` checks, so no
  one-time grant is used up and no decision is recorded). Workers keep no
  access of their own ("access follows the work"): their tasks get it from
  TeamLead when they need it, so there is no step for the worker;
- account: the person's own sign-in (with the account name when the broker
  knows it) or a linked account, the person's own or the team's;
- the team's sign-in app (Google, X), which people sign in through.

The account part comes from the broker (``tool_setup.status``,
``secure_tool_broker.tool_setup_status``) through the access gate. The person
is always the one in the task's signed context, never an argument. Each gap
comes with its fix and who can do it; ``custom_agents.tool_setup_runs`` turns
the gaps into the pieces of a setup run. The check also refreshes the
person's stored last known setup states (``access_control.tool_setup_states``).
"""

from __future__ import annotations

import json
import logging
import os
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from access_control.account_sharing import TeamAccountSharing
from access_control.entities import AgentPrincipal, PersonPrincipal
from access_control.identity import IdentityRejected
from access_control.people import is_administrator
from custom_messages.contracts.access_v2 import AccessLevel, AccountMode, LinkOwnerKind, TargetKind, TargetRefV1
from custom_messages.contracts.credentials_v1 import (
    TOOL_SETUP_ACCOUNT_LINK,
    TOOL_SETUP_OWNER_PERSON,
    TOOL_SETUP_OWNER_TEAM,
    TOOL_SETUP_SIGN_IN,
    TOOL_SETUP_TEAM_SIGN_IN_APP,
)
from access_control.tool_setup_states import SOURCE_SETUP_CHECK, STEP_ACCESS, ToolSetupStates, safely
from custom_tools.runtime_context import get_agent_ref
from id_generation import new_id
from secure_tools.auth_methods import tool_sign_in
from storage.access_repository import AccessRepository

logger = logging.getLogger(__name__)

MAX_TOOLS = 12
BROKER_PROVIDER = "tool_setup"
BROKER_ACTION = "status"
BROKER_TIMEOUT_SECONDS = 45
# Access states.
ALLOWED = "allowed"
NEEDS_REQUEST = "needs_request"
BLOCKED = "blocked"
MISSING = "missing"
NOT_NEEDED = "not_needed"
UNKNOWN = "unknown"
SIGNED_IN = "signed_in"
CONFIGURED = "configured"
# Who can fix a gap.
BY_PERSON = "person"
BY_ADMINISTRATOR = "administrator"
BY_TEAM_LEAD = "team_lead"
# A setup page trusted code opens (set_up_tools); not an action the model writes.
TOOL_SETUP_ACTION = "setup_page"


def _json(payload: dict[str, Any]) -> str:
    return json.dumps(payload, default=str, sort_keys=True)


def _level(level: str) -> AccessLevel:
    try:
        return AccessLevel(str(level or "read").strip().lower())
    except ValueError as exc:
        raise ValueError("level is read, write or full.") from exc


# -- access (authorization on) ---------------------------------------------------------


def _side_state(service: Any, principal: Any, target: TargetRefV1, level: AccessLevel, task_id: str | None,
                person_id: str | None) -> str:
    if isinstance(principal, AgentPrincipal):
        if service.holds(principal, target, level, task_id=task_id, person_id=person_id):
            return ALLOWED
        blocked = service.blocks_on(principal, target, task_id=task_id, person_id=person_id)
        return BLOCKED if blocked else NEEDS_REQUEST
    if service.holds(principal, target, level, task_id=task_id):
        return ALLOWED
    return BLOCKED if service.blocks_on(principal, target, task_id=task_id) else MISSING


def _access(context: dict[str, Any], tool_id: str, level: AccessLevel, worker_type: str) -> dict[str, Any]:
    service = context["service"]
    if service is None:
        return {"team_lead": NOT_NEEDED, "person": NOT_NEEDED, "note": "Authorization is off."}
    target = TargetRefV1(kind=TargetKind.TOOL, id=tool_id)
    person: PersonPrincipal = context["person"]
    lead: AgentPrincipal = context["principal"]
    # Access for one task is bound to the request the task works on.
    work_id = lead.work_id
    access: dict[str, Any] = {
        "team_lead": _side_state(service, lead, target, level, work_id, person.person_id),
        "person": _side_state(service, person, target, level, work_id, None),
    }
    if worker_type:
        # The worker's tasks get it from TeamLead, so TeamLead must be able to hand it on.
        worker = AgentPrincipal(worker_type, worker_type, service.agent_role_of(worker_type), service.team_id)
        blocked = service.blocks_on(worker, target, task_id=work_id, person_id=person.person_id)
        may_hand_on = service.may_give(lead, target, level, task_id=work_id, person_id=person.person_id)
        access["worker"] = {
            "agent_type": worker_type,
            "state": BLOCKED if blocked else ALLOWED if may_hand_on else NEEDS_REQUEST,
        }
    return access


def _access_steps(access: dict[str, Any], tool_id: str, name: str, level: AccessLevel) -> list[dict[str, Any]]:
    steps: list[dict[str, Any]] = []
    if access.get("person") == MISSING:
        steps.append({
            "step": "person_access", "who": BY_ADMINISTRATOR, "fix_with": "tell_person",
            "say": f"You don't have {level.value} access to {name} yet; a team administrator can give it to you.",
        })
    elif access.get("person") == BLOCKED:
        steps.append({
            "step": "person_access", "who": BY_ADMINISTRATOR, "fix_with": "tell_person",
            "say": f"{name} is blocked for you; only a team administrator can remove the block.",
        })
    worker = access.get("worker") or {}
    hand_on = worker.get("state") == NEEDS_REQUEST
    if access.get("team_lead") == NEEDS_REQUEST or hand_on:
        # One request: TeamLead's access, which it may hand on to the worker's
        # tasks when a worker does the work.
        steps.append({
            "step": "team_lead_access", "who": BY_TEAM_LEAD, "fix_with": "request_access", "hand_on": hand_on,
            "call": f"request_access(tool_id={tool_id!r}, level={level.value!r}"
            + (", hand_on=True" if hand_on else "")
            + ", reason='<why, in plain words>')",
        })
    if BLOCKED in {access.get("team_lead"), worker.get("state")}:
        steps.append({
            "step": "agent_blocked", "who": BY_ADMINISTRATOR, "fix_with": "tell_person",
            "say": f"An agent is blocked from {name}; only a person who manages access can remove the block.",
        })
    return steps


# -- account (the broker) -------------------------------------------------------------


def _broker_status(tool_ids: list[str]) -> tuple[dict[str, dict[str, Any]], str]:
    """The broker's per-tool account states, and a problem note when it could not answer."""

    from custom_tools.secure_cli_tool import trusted_broker_context

    base_url = str(os.getenv("SECURE_TOOL_BROKER_INTERNAL_BASE_URL") or "").rstrip("/")
    context = trusted_broker_context()
    if not base_url or context is None:
        return {}, "The secure tool broker cannot be reached from this task, so sign-ins and links are unknown."
    body = {
        "provider": BROKER_PROVIDER,
        "action": BROKER_ACTION,
        "params": {"tool_ids": tool_ids},
        "reason": "Check whether the tools a request needs are set up for the person.",
        "context": {**context, "invocation_id": new_id("tool_setup_chk")},
    }
    token = str(os.getenv("SECURE_TOOL_BROKER_TOKEN") or "")
    request = Request(
        f"{base_url}/v1/tools/{BROKER_PROVIDER}",
        data=json.dumps(body).encode("utf-8"),
        headers={"Content-Type": "application/json", **({"Authorization": f"Bearer {token}"} if token else {})},
        method="POST",
    )
    try:
        with urlopen(request, timeout=BROKER_TIMEOUT_SECONDS) as response:
            payload = json.loads(response.read().decode("utf-8", errors="replace") or "{}")
    except HTTPError as exc:
        logger.warning("The broker refused a tool setup check. event=tool_setup_check_refused http_status=%s", exc.code)
        return {}, f"The secure tool broker refused the check (HTTP {exc.code}); sign-ins and links are unknown."
    except (URLError, TimeoutError, OSError, ValueError) as exc:
        logger.warning("The broker did not answer a tool setup check. event=tool_setup_check_failed error=%s", exc)
        return {}, "The secure tool broker did not answer; sign-ins and links are unknown. Try again shortly."
    tools = ((payload.get("content") or {}).get("tools") or []) if isinstance(payload, dict) else []
    return {str(item.get("tool_id")): item for item in tools if isinstance(item, dict)}, ""


def _account_ready(tool_def: Any, broker: dict[str, Any], owners: tuple[str, ...]) -> bool:
    sign_in = (broker.get("sign_in") or {}).get("state")
    link = broker.get("link") or {}
    if tool_sign_in(tool_def.tool_id) is not None:
        # X also takes the team's linked account when the person has not signed
        # in; the broker reports it only while the team shares X.
        return sign_in == SIGNED_IN or bool(link.get("team"))
    if tool_def.account is AccountMode.LINKED_CREDENTIAL:
        # Only the owners the tool and the team's sharing allow count.
        return bool(("person" in owners and link.get("own")) or ("team" in owners and link.get("team")))
    return True


def _sign_in_steps(tool_id: str, broker: dict[str, Any], is_admin: bool) -> list[dict[str, Any]]:
    sign_in = tool_sign_in(tool_id)
    if sign_in is None:
        return []
    name = sign_in.display_name
    steps: list[dict[str, Any]] = []
    if broker.get("team_sign_in_app") == MISSING:
        steps.append({
            "step": TOOL_SETUP_TEAM_SIGN_IN_APP, "who": BY_ADMINISTRATOR,
            "fix_with": TOOL_SETUP_ACTION if is_admin else "tell_person_and_stop",
            "kind": TOOL_SETUP_TEAM_SIGN_IN_APP, "owner": TOOL_SETUP_OWNER_TEAM,
            "say": (
                f"First the team's {name} sign-in app needs setting up once; you're an administrator, so I can "
                "open that page for you."
                if is_admin
                else f"{name} isn't set up for the team yet: a team administrator or owner needs to set up the "
                f"team's {name} sign-in app ({sign_in.team_client_command}). After that you can sign in."
            ),
        })
    state = (broker.get("sign_in") or {}).get("state")
    if state == UNKNOWN:
        steps.append({
            "step": "sign_in_unknown", "who": BY_TEAM_LEAD, "fix_with": "try_the_tool",
            "note": "The sign-in could not be checked just now; try the tool, and offer a sign-in if it is refused.",
        })
    elif state != SIGNED_IN:
        steps.append({
            "step": TOOL_SETUP_SIGN_IN, "who": BY_PERSON, "fix_with": TOOL_SETUP_ACTION,
            "kind": TOOL_SETUP_SIGN_IN, "owner": TOOL_SETUP_OWNER_PERSON,
            "say": f"To use {name}, I need you to sign in once.",
        })
    return steps


def _team_only_link_step(tool_id: str, name: str, is_admin: bool) -> list[dict[str, Any]]:
    """A tool that uses only the team's account: an administrator links it."""

    return [{
        "step": "team_account_link", "who": BY_ADMINISTRATOR,
        "fix_with": TOOL_SETUP_ACTION if is_admin else "tell_person_and_stop",
        "kind": TOOL_SETUP_ACCOUNT_LINK, "owner": TOOL_SETUP_OWNER_TEAM,
        "say": f"{name} uses the team's account, which a team administrator links with /link {tool_id} team.",
    }]


def _link_steps(
    tool_id: str,
    name: str,
    is_admin: bool,
    owners: tuple[str, ...] = (LinkOwnerKind.PERSON.value, LinkOwnerKind.TEAM.value),
    *,
    private: bool = False,
) -> list[dict[str, Any]]:
    """The link steps for the owners that count; ``private``: the team keeps the service private."""

    if "person" not in owners:
        return _team_only_link_step(tool_id, name, is_admin)
    steps = [{
        "step": TOOL_SETUP_ACCOUNT_LINK, "who": BY_PERSON, "fix_with": TOOL_SETUP_ACTION,
        "kind": TOOL_SETUP_ACCOUNT_LINK, "owner": TOOL_SETUP_OWNER_PERSON,
        "say": (
            f"The team keeps {name} private, so agents use your own account: choose which of your "
            "saved accounts it should use."
            if private
            else f"To use {name}, choose which of your saved accounts it should use."
        ),
    }]
    if "team" not in owners:
        return steps
    steps.append({
        # Another way to fix the same gap, not a second piece.
        "step": "team_account_link", "who": BY_ADMINISTRATOR, "alternative_to": TOOL_SETUP_ACCOUNT_LINK,
        "fix_with": TOOL_SETUP_ACTION if is_admin else "tell_person",
        "kind": TOOL_SETUP_ACCOUNT_LINK, "owner": TOOL_SETUP_OWNER_TEAM,
        "say": f"Or the team's account can be linked for everyone ({'you can do it' if is_admin else 'a team administrator can do it'} "
        f"with /link {tool_id} team).",
    })
    return steps


def _account_steps(
    tool_def: Any, broker: dict[str, Any], is_admin: bool, owners: tuple[str, ...]
) -> list[dict[str, Any]]:
    """What is missing for the tool's account; ``owners`` from the team's sharing (``access_control.account_sharing``)."""

    if _account_ready(tool_def, broker, owners):
        return []
    if tool_sign_in(tool_def.tool_id) is not None:
        return _sign_in_steps(tool_def.tool_id, broker, is_admin)
    private = tool_def.sharing_choice and LinkOwnerKind.TEAM not in owners
    return _link_steps(tool_def.tool_id, tool_def.display_name, is_admin, owners, private=private)


# -- the tool ---------------------------------------------------------------------------


def _context() -> dict[str, Any]:
    from custom_agents.access_runtime import runtime_access, task_access

    agent = get_agent_ref()
    if agent is None:
        raise RuntimeError("No agent is running this tool.")
    access = runtime_access(agent)
    principal, person, task_id = task_access(agent, access)
    if person is None:
        raise IdentityRejected("This task works for no person, so there is nobody to set tools up for.")
    store = AccessRepository(agent.task_manager.storage_settings)
    return {
        "catalog": access.catalog,
        "service": access.service,
        "principal": principal,
        "person": person,
        "task_id": task_id,
        "states": ToolSetupStates(store),
        "sharing": TeamAccountSharing(store, person.team_id),
    }


def _known_state(context: dict[str, Any], tool_def: Any, access: dict[str, Any]) -> dict[str, Any] | None:
    """Record the person's own missing access, then return the stored last known state.

    The broker's check already recorded the account part
    (``secure_tool_broker.tool_setup_status``); only this side knows the
    person's access. States cover tools that act with an account.
    """

    if tool_def.account is AccountMode.NONE:
        return None
    states: ToolSetupStates = context["states"]
    person = context["person"]
    if access.get("person") in {MISSING, BLOCKED}:
        safely(
            lambda: states.note_needs_setup(
                person.team_id, person.person_id, tool_def.tool_id, step=STEP_ACCESS, source=SOURCE_SETUP_CHECK
            ),
            what="person_access",
            tool_id=tool_def.tool_id,
            person_id=person.person_id,
        )
    stored = states.get(person.team_id, person.person_id, tool_def.tool_id)
    return stored.model_dump(
        mode="json", include={"state", "how", "account_label", "missing_step", "fixed_by", "last_worked_at_utc"}
    ) if stored else None


def _tool_report(context: dict[str, Any], tool_id: str, broker: dict[str, Any], level: AccessLevel,
                 worker_type: str) -> dict[str, Any]:
    tool_def = context["catalog"].tools.get(tool_id)
    if tool_def is None:
        return {"tool_id": tool_id, "ready": False, "next_steps": [
            {"step": "unknown_tool", "fix_with": "find_tools", "note": "Not in the tool catalog; search with find_tools."}
        ]}
    is_admin = is_administrator(context["person"].roles)
    access = _access(context, tool_id, level, worker_type)
    steps = _access_steps(access, tool_id, tool_def.display_name, level)
    if tool_def.account is not AccountMode.NONE:
        owners = context["sharing"].link_owners(tool_def)
        steps += _account_steps(tool_def, broker, is_admin, owners) if broker else [
            {"step": "account_unknown", "fix_with": "try_the_tool", "note": "The account part could not be checked."}
        ]
    report: dict[str, Any] = {
        "tool_id": tool_id,
        "name": tool_def.display_name,
        "access": access,
        "ready": not steps,
        "next_steps": steps,
    }
    for key in ("sign_in", "link", "team_sign_in_app"):
        if key in broker:
            report[key] = broker[key]
    known = _known_state(context, tool_def, access)
    if known is not None:
        report["known_state"] = known
    return report


def check_tools(tool_ids: list[str], level: str, agent_type: str) -> tuple[dict[str, Any], list[dict[str, Any]], str]:
    """The check of each tool (in the given order), its context, and a note when the broker did not answer."""

    wanted = list(dict.fromkeys(str(item or "").strip() for item in (tool_ids or []) if str(item or "").strip()))
    if not wanted:
        raise ValueError("Name at least one tool id, for example tool_ids=['cli:github'].")
    if len(wanted) > MAX_TOOLS:
        raise ValueError(f"Set up at most {MAX_TOOLS} tools at once.")
    needed = _level(level)
    context = _context()
    broker, problem = _broker_status(wanted)
    reports = [
        _tool_report(context, tool_id, broker.get(tool_id, {}), needed, str(agent_type or "").strip())
        for tool_id in wanted
    ]
    return context, reports, problem


__all__ = [
    "BY_ADMINISTRATOR",
    "BY_PERSON",
    "BY_TEAM_LEAD",
    "MAX_TOOLS",
    "TOOL_SETUP_ACTION",
    "check_tools",
]
