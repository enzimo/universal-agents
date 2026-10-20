"""One access decision point for every function tool an agent calls.

A Strands ``BeforeToolCallEvent`` hook, installed for every agent and run last
(so an earlier cancellation cannot use up a one-time grant), resolves each call
through the tool catalog and asks the access service (``access_control``):

- calls another enforcement point decides are skipped: ``secure_cli`` (the
  broker's access gate) and brokered MCP operations (the broker's MCP runtime);
- who the task works for comes only from its signed task context or its
  schedule run identity, checked in this process
  (``access_control.task_identity``); work with no person runs only for
  built-in system agents, on their duties;
- the agent's role comes from its own settings: the team lead, a built-in
  system agent (``access_role: system``), or a worker;
- with authorization off, every catalog tool is allowed (decision D8: trusted
  single-person mode); the broker, the sandbox and path rules still apply;
- each call is decided with its own stable id (``call_operation_id``). A tool
  that waits for a person inside itself pauses with a Strands interrupt, and
  Strands runs the call again from its start when it resumes, this hook
  included; the id lets the access service see that the call already took
  its one-time use, so the resumed call keeps it, and no other call gets it.

A refusal cancels the call with the tool, operation, level, the reason and the
exact next step; there are no denial receipts.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
from dataclasses import dataclass, replace
from functools import lru_cache, partial
from typing import Any, Mapping, cast

from strands.hooks import BeforeToolCallEvent, HookRegistry
from strands.tools.structured_output.structured_output_tool import StructuredOutputTool

from access_catalog.catalog import CatalogSource, ToolCatalog
from access_control.cedar import CedarAccessClient
from access_control.decisions import AccessService, HandDownRefused
from access_control.entities import (
    AGENT_ROLE_SYSTEM,
    AGENT_ROLE_TEAM_LEAD,
    AGENT_ROLE_WORKER,
    AgentPrincipal,
    PersonPrincipal,
    agent_role_resolver,
)
from access_control.identity import DEFAULT_CONTEXT_LIFETIME, ContextVerifier, IdentityRejected
from access_control.inbox import StoredAccessInbox
from access_control.requests import AccessRequests
from access_control.task_identity import ScheduleRunLookup, job_schedule_id, resolve_task_identity
from custom_messages.contracts.access_v2 import (
    AccessDecisionV1,
    AccessNoticeKind,
    AccessNoticeV1,
    OperationResolutionV1,
    TaskContextV1,
    ToolKind,
)
from id_generation import new_id
from custom_messages.enums import ProgressState
from custom_messages.contracts.runtime_v1 import TEAM_ARCHITECT_AGENT_TYPE
from storage.access_repository import AccessRepository
from storage.config import StorageSettings

logger = logging.getLogger(__name__)

ACCESS_HOOK_ORDER = float("inf")
BROKER_DISPATCH = "broker"
REASON_ACCESS_DENIED = "access_denied"


class AuthorizationOff(RuntimeError):
    """Access requests and hand-downs do not exist with authorization off (D8)."""


@dataclass
class RuntimeAccess:
    """Access decisions for one team in this process (shared by its agents)."""

    team_id: str
    enabled: bool
    catalogs: CatalogSource
    verifier: ContextVerifier
    service: AccessService | None
    # Live checks of scheduled runs that carry a schedule permit (task_identity).
    schedule_runs: ScheduleRunLookup | None = None

    @property
    def catalog(self) -> ToolCatalog:
        catalog = self.catalogs.current()
        if self.service is not None and self.service.catalog is not catalog:
            self.service.catalog = catalog
        return catalog


@lru_cache(maxsize=8)
def _runtime_access(
    team_id: str,
    enabled: bool,
    instance_root: str | None,
    storage: StorageSettings,
    cedar_binary: str | None,
) -> RuntimeAccess:
    catalogs = CatalogSource(instance_root)
    service = None
    if enabled:
        service = AccessService(
            store=AccessRepository(storage),
            catalog=catalogs.current(),
            cedar=CedarAccessClient(binary=cedar_binary),
            team_id=team_id,
            agent_role_of=agent_role_resolver(TEAM_ARCHITECT_AGENT_TYPE),
            lead_agent_type=TEAM_ARCHITECT_AGENT_TYPE,
        )
    return RuntimeAccess(
        team_id, enabled, catalogs, ContextVerifier.from_env(), service, ScheduleRunLookup.from_storage(storage)
    )


_TRUE_WORDS = frozenset({"1", "true", "yes", "on"})
_FALSE_WORDS = frozenset({"0", "false", "no", "off"})


def authorization_enabled(config: Mapping[str, Any] | None) -> bool:
    """Whether authorization is on (``authorization.enabled``; on by default).

    The one reader of the switch for agents, their tools, team construction
    and schedule checks, so they cannot disagree. Off is trusted single-person
    mode (decision D8). A value that is not a yes/no word is refused rather
    than guessed, because guessing "on" or "off" would silently change who
    may do what.
    """

    value = dict(config or {}).get("enabled", True)
    if isinstance(value, bool):
        return value
    word = str(value).strip().lower()
    if word in _TRUE_WORDS:
        return True
    if word in _FALSE_WORDS:
        return False
    raise ValueError(f"authorization.enabled must be true or false, not {value!r}")


def runtime_access(agent: Any) -> RuntimeAccess:
    config = dict(getattr(agent, "authorization_config", {}) or {})
    storage = getattr(getattr(agent, "task_manager", None), "storage_settings", None)
    if storage is None:
        raise RuntimeError("The agent has no task storage, so its access cannot be decided.")
    return _runtime_access(
        str(getattr(agent, "team_id", "") or ""),
        authorization_enabled(config),
        str(getattr(agent, "instance_root", "") or "") or None,
        storage,
        str(config.get("cedar_binary_path") or "") or None,
    )


def agent_principal(agent: Any) -> AgentPrincipal:
    if bool(getattr(agent, "is_team_lead", False)):
        role = AGENT_ROLE_TEAM_LEAD
    elif str(getattr(agent, "access_role", "") or "") == AGENT_ROLE_SYSTEM:
        role = AGENT_ROLE_SYSTEM
    else:
        role = AGENT_ROLE_WORKER
    return AgentPrincipal(
        str(getattr(agent, "id", "") or ""),
        str(getattr(agent, "named_agent_type", "") or ""),
        role,
        str(getattr(agent, "team_id", "") or ""),
    )


def _active_identity(agent: Any, access: RuntimeAccess) -> TaskContextV1:
    """Who the active task works for (``access_control.task_identity``); raises when nothing proves it."""

    from custom_tools.runtime_context import active_schedule_run, active_signed_task_context

    return resolve_task_identity(
        verifier=access.verifier,
        team_id=access.team_id,
        task_context=active_signed_task_context(agent),
        schedule_run=active_schedule_run(agent),
        schedule_runs=access.schedule_runs,
    )


def task_access(agent: Any, access: RuntimeAccess) -> tuple[AgentPrincipal, PersonPrincipal | None, str | None]:
    """Who acts in the active task: the agent (with the scheduled job its task
    is a run of, ``task_identity.job_schedule_id``, and the request it works
    on as ``work_id``), the person it
    works for (None for system work), and the agent's own task id. Access for
    one task is matched on ``work_id``; the task id only pauses and resumes.

    Everything comes from the task's verified identity (``_active_identity``),
    never from message fields, so grants tied to a person or a schedule count
    only in that person's work and that schedule's runs. Raises
    ``IdentityRejected`` when the task has no valid signed context or
    schedule run identity.
    """

    from custom_tools.runtime_context import active_task_context

    task_id, _meta = active_task_context(agent)
    context = _active_identity(agent, access)
    schedule_id = job_schedule_id(context, access.schedule_runs, lead_agent_type=TEAM_ARCHITECT_AGENT_TYPE)
    principal = replace(agent_principal(agent), schedule_id=schedule_id, work_id=context.context_id)
    if context.person_id is None:
        return principal, None, task_id
    return principal, PersonPrincipal(context.person_id, context.team_id, context.person_roles), task_id


def task_person(agent: Any, access: RuntimeAccess) -> tuple[PersonPrincipal | None, str | None]:
    """The person the active task works for (None for system work) and its task id (see ``task_access``)."""

    _principal, person, task_id = task_access(agent, access)
    return person, task_id


def _claimed_person_id(meta: Any) -> str | None:
    """The person a message's gateway context names (``canonical_user_id``): a claim, never proof."""

    claimed = getattr(getattr(meta, "gateway_context", None), "canonical_user_id", None)
    return str(claimed or "").strip() or None


def _report_person_claim(
    agent: Any,
    meta: Any,
    *,
    verified_person_id: str | None,
    unverified_reason: object | None = None,
    context_id: str | None = None,
) -> None:
    """Log when the person a message names is not the one its task verifiably works for.

    The verified person (or nobody) is always the one used; this only makes the
    difference visible. ``unverified_reason`` is set when nothing valid proves
    whom the task works for. Nothing is logged when the message names nobody.
    """

    claimed = _claimed_person_id(meta)
    if claimed is None:
        return
    if unverified_reason is not None:
        logger.warning(
            "A task names a person without a valid signed context; treating it as work for nobody. "
            "event=unverified_person_ignored agent_id=%s claimed_person_id=%s reason=%s",
            getattr(agent, "id", None),
            claimed,
            unverified_reason,
        )
    elif claimed != verified_person_id:
        logger.warning(
            "A task names a different person than its verified identity; using the verified one. "
            "event=claimed_person_mismatch agent_id=%s claimed_person_id=%s verified_person_id=%s context_id=%s",
            getattr(agent, "id", None),
            claimed,
            verified_person_id,
            context_id,
        )


def verified_person_id(agent: Any) -> str | None:
    """The person the active task works for, taken from its signed context only.

    None for system work and when the task has no valid signed context, so
    private data and a person's folders stay out of reach rather than trusting
    a person id carried in message fields. When such a field names someone
    that the signed context does not, the mismatch is logged.
    """

    from custom_tools.runtime_context import active_task_context

    try:
        principal, person, _task_id = task_access(agent, runtime_access(agent))
    except (IdentityRejected, RuntimeError) as exc:
        _task_id, meta = active_task_context(agent)
        _report_person_claim(agent, meta, verified_person_id=None, unverified_reason=exc)
        return None
    person_id = person.person_id if person is not None else None
    _task_id, meta = active_task_context(agent)
    _report_person_claim(agent, meta, verified_person_id=person_id, context_id=principal.work_id)
    return person_id


def verified_meta_person_id(agent: Any, meta: Any) -> str | None:
    """The person a task with this message meta works for, from its verified identity only.

    For trusted code that reads a task's message outside a tool call, such as
    context assembly and after-turn memory upkeep (``custom_agents.memory_plugins``).
    It checks the meta like ``resolve_meta_identity`` (the signed task context
    or the schedule run identity, ``access_control.task_identity``), so it gives
    the same person ``verified_person_id`` gives the task's tools, in both
    authorization modes. None for system work and when nothing valid proves
    whom the task works for. The ``canonical_user_id`` in the message's gateway
    context is never used; when it names someone else, or nobody is verified,
    that is logged.
    """

    try:
        identity = _resolve_meta(runtime_access(agent), meta)
    except (IdentityRejected, ValueError, RuntimeError) as exc:
        _report_person_claim(agent, meta, verified_person_id=None, unverified_reason=exc)
        return None
    _report_person_claim(agent, meta, verified_person_id=identity.person_id, context_id=identity.context_id)
    return identity.person_id


@dataclass(frozen=True)
class ActingAgent:
    """Who is acting in a trusted service call: the agent and its verified person."""

    agent: AgentPrincipal
    person: PersonPrincipal | None
    task_id: str | None

    @property
    def person_id(self) -> str | None:
        return self.person.person_id if self.person else None


def acting_agent(agent: Any) -> ActingAgent:
    """The agent and the person its active task works for (from the signed context).

    Raises ``IdentityRejected`` when the task carries no valid signed context,
    in both authorization modes: services bind their plans to this person.
    """

    principal, person, task_id = task_access(agent, runtime_access(agent))
    return ActingAgent(principal, person, task_id)


def decided_elsewhere(catalog: ToolCatalog, function: str, resolution: OperationResolutionV1) -> bool:
    """Calls the broker decides: its dispatcher and brokered MCP operations."""

    if catalog.function_dispatchers.get(function) == BROKER_DISPATCH:
        return True
    tool = catalog.tools.get(resolution.tool_id)
    return tool is not None and tool.kind is ToolKind.MCP


def decide_call(
    agent: Any, function: str, arguments: Mapping[str, Any] | None, *, operation_id: str | None = None
) -> AccessDecisionV1 | None:
    """The decision for one function call, or None when this point does not decide it.

    ``operation_id`` is the call's id (``call_operation_id``); a one-time use
    the decision takes is bound to it. Trusted code deciding outside a tool
    call passes none.
    """

    access = runtime_access(agent)
    catalog = access.catalog
    resolution = catalog.resolve_function(function, arguments)
    if not access.enabled or decided_elsewhere(catalog, function, resolution):
        return None
    assert access.service is not None
    try:
        principal, person, task_id = task_access(agent, access)
    except (IdentityRejected, ValueError) as exc:
        return AccessDecisionV1(
            allowed=False,
            tool_id=resolution.tool_id,
            operation=resolution.operation,
            level=resolution.level,
            reason=f"Nobody verifiably asked for this task ({exc}).",
            next_step="Tell the person; their request needs to come through the gateway again.",
        )
    # A worker's task gets what the team lead may hand on at once ("access follows the work").
    return access.service.decide(
        agent=principal,
        person=person,
        resolution=resolution,
        task_id=principal.work_id,
        hand_on_for_task=True,
        operation_id=operation_id,
    )


# Prefix of the ids call_operation_id makes, so a reader of the store knows what they name.
TOOL_CALL_OPERATION_PREFIX = "tool_call:"


def _asked_for(message: Any, tool_use_id: str) -> bool:
    """Whether this message is a model step that asked for the tool use."""

    if not isinstance(message, Mapping) or message.get("role") != "assistant":
        return False
    tool_uses = (block.get("toolUse") for block in message.get("content") or () if isinstance(block, Mapping))
    return any(isinstance(use, Mapping) and use.get("toolUseId") == tool_use_id for use in tool_uses)


def _step_tracking_id(messages: Any, tool_use_id: str) -> str | None:
    """The ``tracking_id`` of the latest model step (assistant message) that asked for this tool use."""

    step = next((message for message in reversed(list(messages or ())) if _asked_for(message, tool_use_id)), None)
    tracking_id = step.get("tracking_id") if step is not None else None
    return str(tracking_id) if tracking_id else None


def call_operation_id(agent: Any, event: BeforeToolCallEvent) -> str | None:
    """The stable id of one tool call, which a one-time use it takes is bound to.

    Strands runs a call that paused inside its tool again from its start when
    it resumes, and this hook decides it again; the same id lets the access
    service recognize the use the call already took
    (``AccessRepository.consume``). It covers the agent, the model step that
    asked for the call (the step message's ``tracking_id``: a UUID Strands
    gives every message and keeps with it, also in the paused call it
    replays) and the call's tool use id (unique within one step, which
    Strands' own resume relies on). The tool use id alone is not enough: some
    model servers repeat their ids in later steps. The parts are hashed to a
    fixed length, because tool use ids can be long. Sibling calls of one step
    and calls of later steps get other ids, so they never pass on that use.
    An agent's id is new each time it starts, so a call's id never outlives
    its process; neither does a paused call (no Strands session is stored:
    after a restart the task continues with a fresh model turn).

    None when no model step in the agent's history asked for the call: it
    cannot be resumed, so its use is bound to no call. That is logged, because
    every model-driven call has a step.
    """

    tool_use_id = str(event.tool_use.get("toolUseId") or "")
    step = _step_tracking_id(getattr(event.agent, "messages", None), tool_use_id) if tool_use_id else None
    if step is None:
        logger.warning(
            "A tool call is not in the agent's model history, so a one-time use it takes is bound to no call. "
            "event=tool_call_without_model_step agent_id=%s tool=%s tool_use_id=%s",
            getattr(agent, "id", None),
            event.tool_use.get("name"),
            tool_use_id or None,
        )
        return None
    parts = "\n".join((str(getattr(agent, "id", "") or ""), step, tool_use_id))
    return TOOL_CALL_OPERATION_PREFIX + hashlib.sha256(parts.encode("utf-8")).hexdigest()


def verified_task_context(agent: Any, access: RuntimeAccess | None = None) -> TaskContextV1 | None:
    """Who the active task works for, checked (signed context or schedule run identity), or None."""

    access = access or runtime_access(agent)
    try:
        return _active_identity(agent, access)
    except (IdentityRejected, ValueError):
        return None


def resolve_meta_identity(agent: Any, meta: Any) -> TaskContextV1 | None:
    """Who a task with this message meta works for, checked like the active task's, or None.

    For trusted code that handles a message before it becomes the active task
    (``task_context_runtime.sign_system_escalation``).
    """

    try:
        access = runtime_access(agent)
    except RuntimeError as exc:  # no task storage: nothing can be checked here
        logger.warning("Could not check whom a message's task works for. event=task_identity_unchecked reason=%s", exc)
        return None
    try:
        return _resolve_meta(access, meta)
    except (IdentityRejected, ValueError):
        return None


def _resolve_meta(access: RuntimeAccess, meta: Any) -> TaskContextV1:
    """Who a task with this message meta works for (``resolve_task_identity``); raises when nothing proves it."""

    gateway_context = getattr(meta, "gateway_context", None)
    return resolve_task_identity(
        verifier=access.verifier,
        team_id=access.team_id,
        task_context=getattr(gateway_context, "task_context", None),
        schedule_run=getattr(gateway_context, "schedule_run", None),
        schedule_runs=access.schedule_runs,
    )


def verified_context_id(agent: Any, access: RuntimeAccess | None = None) -> str | None:
    """The active task's verified signed context id, or None when it has none.

    Delegated tasks carry their root task's signed context unchanged, so the
    id ties a worker's task to the person's request it serves.
    """

    context = verified_task_context(agent, access)
    return context.context_id if context is not None else None


def runtime_requests(agent: Any) -> AccessRequests:
    """Access requests for the agent's team; needs authorization on.

    Requests are shown on the task's access workflow as they are made
    (``custom_agents.access_workflows.WorkflowRequestWatcher``).
    """

    from custom_agents.access_workflows import WorkflowRequestWatcher, workflow_service_for

    access = runtime_access(agent)
    if access.service is None:
        raise AuthorizationOff("Authorization is off, so every tool is available without asking.")
    # Reading the catalog reloads it when instance tools or MCP servers changed
    # (an install, a discovery), so requests and hand-downs name current tools.
    _ = access.catalog
    return AccessRequests(
        service=access.service,
        inbox=StoredAccessInbox(access.service.store),
        lead_agent_type=TEAM_ARCHITECT_AGENT_TYPE,
        watcher=WorkflowRequestWatcher(
            lambda: workflow_service_for(agent),
            lambda: verified_context_id(agent, access),
        ),
    )


def send_access_notice(
    agent: Any,
    *,
    kind: AccessNoticeKind,
    title: str,
    summary: str,
    person_id: str | None = None,
    roles: tuple[str, ...] = (),
) -> None:
    """Store a silent Inbox notice (decision D3); nothing with authorization off.

    A notice informs and grants nothing, so a failure to store it is logged
    and never stops the change it reports.
    """

    try:
        requests = runtime_requests(agent)
        requests.inbox.send_notice(
            AccessNoticeV1(
                notice_id=new_id("access_notice"),
                team_id=requests.service.team_id,
                kind=kind,
                recipient_person_id=person_id,
                recipient_roles=roles,
                title=title[:400],
                summary=summary[:4000],
                created_at_utc=requests.service.clock(),
            )
        )
    except AuthorizationOff:
        return
    except Exception:  # noqa: BLE001 - see docstring
        logger.exception("Could not store an access notice. event=access_notice_failed kind=%s", kind.value)


def send_runtime_notice_once(
    agent: Any,
    *,
    notice_id: str,
    kind: AccessNoticeKind,
    title: str,
    summary: str,
    person_id: str | None = None,
    roles: tuple[str, ...] = (),
) -> bool:
    """Store an Inbox notice from trusted runtime code once per ``notice_id``, in both authorization modes.

    For reports about the runtime itself (an MCP server that stopped answering,
    schedules to review), which people need whether or not access control is
    on; the Inbox shows notices in both modes. Unlike ``send_access_notice``,
    the caller derives ``notice_id`` from what it reports, so the same report
    from several agents or from every maintenance run reaches people once.
    Returns True when stored; a failure is logged and returns False.
    """

    from datetime import datetime, timezone

    try:
        stored = AccessRepository(_storage_of(agent)).create_notice_if_absent(
            AccessNoticeV1(
                notice_id=notice_id[:120],
                team_id=str(getattr(agent, "team_id", "") or ""),
                kind=kind,
                recipient_person_id=person_id,
                recipient_roles=roles,
                title=title[:400],
                summary=summary[:4000],
                created_at_utc=datetime.now(timezone.utc),
            )
        )
    except Exception:  # noqa: BLE001 - a notice informs; failing to store it never stops the work it reports
        logger.exception(
            "Could not store a runtime notice. event=runtime_notice_failed kind=%s notice_id=%s", kind.value, notice_id
        )
        return False
    if stored:
        logger.info("Stored a runtime notice. event=runtime_notice_stored kind=%s notice_id=%s", kind.value, notice_id)
    return stored


def _storage_of(agent: Any) -> StorageSettings:
    storage = getattr(getattr(agent, "task_manager", None), "storage_settings", None)
    if storage is None:
        raise RuntimeError("The agent has no task storage.")
    return cast(StorageSettings, storage)


def refusal_text(decision: AccessDecisionV1) -> str:
    return json.dumps(
        {
            "status": "access_denied",
            "reason_code": REASON_ACCESS_DENIED,
            "tool_id": decision.tool_id,
            "operation": decision.operation,
            "level": decision.level.value,
            "reason": decision.reason,
            "next_step": decision.next_step
            or f"Call request_access with tool_id={decision.tool_id!r} and level={decision.level.value!r}, "
            "and say why you need it.",
        },
        sort_keys=True,
    )


def _ended_request_id(agent: Any, task_id: str) -> str | None:
    """The request (signed context id) a request's first task works on, or None for other tasks.

    Access for one task is bound to the request, which every task of it
    carries; it ends when the request's first task ends: the team lead's task
    for a person's message, or a scheduled run's task, the ones no other task
    handed on (``parent_task_id`` is empty). The id is read without checking
    the signature again: ending access takes nothing a person gave beyond the
    request, and a context that expired meanwhile must still end its access.
    """

    info = agent.task_manager.get_task_info(task_id)
    if info is None or getattr(info.task, "parent_task_id", None):
        return None
    return request_id_of(getattr(info, "meta", None))


def request_id_of(meta: Any) -> str | None:
    """The request a stored task belongs to: its signed context id, or its schedule run's; None without one.

    Every task of a request carries the request's context unchanged, so the
    id ties a worker's task, or a plan step, to the person's request. It is
    read without checking the signature again: the runtime checked the
    context when it admitted the task, and callers only compare it with the
    verified id of the active task (``verified_context_id``) or end what the
    request was given, which must work after the context expired too.
    """

    gateway_context = getattr(meta, "gateway_context", None)
    signed = getattr(gateway_context, "task_context", None)  # SignedTaskContextV1
    if signed is not None:
        return str(signed.context.context_id)
    run = getattr(gateway_context, "schedule_run", None)  # ScheduleRunIdentityV1
    return str(run.context_id) if run is not None else None


def _clean_up_ended_task(agent: Any, task_id: str, state: ProgressState) -> None:
    """When a task ends: withdraw the access requests it waited on; when it is a request's first task, end the
    access bound to the request (``_ended_request_id``).

    Runs in the task transaction. A failure is logged and the task still ends;
    its reviews stay until decided, and its grants are ended by the next
    startup check (``end_grants_of_finished_tasks``).
    """

    requests = _withdraw_waiting_requests(agent, task_id, why=f"The task ended ({state.value}) first.")
    if requests is None:
        return
    try:
        request_id = _ended_request_id(agent, task_id)
        if request_id:
            requests.service.end_task_grants([request_id])
    except Exception:  # noqa: BLE001 - see above
        logger.exception("Could not end the access of an ended task. event=task_grants_end_failed task_id=%s", task_id)


def _clean_up_released_task(agent: Any, task_id: str) -> None:
    """When another agent took the task over: withdraw the access requests this agent's run of it waited on.

    The task-release callback (``TaskManager.add_task_release_callback``,
    2026-10-17). The task goes on with its new holder, which asks again for
    itself, so the person is not asked twice. The access bound to the request
    stays: the task, and the request it belongs to, have not ended.
    """

    _withdraw_waiting_requests(agent, task_id, why="The task moved to another agent, which asks again if it needs to.")


def _withdraw_waiting_requests(agent: Any, task_id: str, *, why: str) -> Any:
    """Withdraw this agent's access requests waiting for the task; the request service, or None without one."""

    try:
        requests = runtime_requests(agent)
        waiting = requests.service.store.waiting_for_task(agent.team_id, agent.id, task_id)
    except AuthorizationOff:
        return None
    except Exception:  # noqa: BLE001 - the task still ends or moves; its reviews stay until decided
        logger.exception("Could not look up access requests of an ended task. event=access_request_lookup_failed task_id=%s", task_id)
        return None
    for request in waiting:
        try:
            requests.withdraw(request.request_id, why=why)
        except HandDownRefused:
            continue  # decided meanwhile
        except Exception:  # noqa: BLE001 - see above
            logger.exception(
                "Could not withdraw the access request of an ended task. event=access_request_withdraw_failed "
                "request_id=%s",
                request.request_id,
            )
    return requests


def end_grants_of_finished_tasks(service: AccessService, archive: Any) -> list[str]:
    """End access for one task whose request can no longer run (the team lead's startup check).

    The task-end callback normally ends it when the request's first task ends
    (``_clean_up_ended_task``). This catches what it missed: a process that
    stopped before the callback, grants bound to one agent task (before access
    for one task covered the whole request) whose task the archive has as
    finished, and access older than a signed task context lives
    (``DEFAULT_CONTEXT_LIFETIME``), which no task can use any more. Each one is
    logged as a warning, because a missed callback is an anomaly worth seeing.
    """

    oldest = service.clock() - DEFAULT_CONTEXT_LIFETIME
    finished: list[str] = []
    for grant in service.store.task_grants(service.team_id):
        if grant.created_at_utc <= oldest:
            why, status = "older_than_any_task_context", None
        else:
            record = archive.get_archived_task(str(grant.task_id), owner_agent_id=None, include_all_agents=True)
            if record is None:
                continue  # still running, or a request that is not over
            why, status = "task_finished", getattr(record, "status", None)
        logger.warning(
            "Access for one task outlived its task; ending it now. event=task_grant_outlived_task grant_id=%s "
            "task_id=%s why=%s task_status=%s subject=%s:%s target=%s",
            grant.grant_id,
            grant.task_id,
            why,
            status,
            grant.subject.kind.value,
            grant.subject.id,
            grant.target.id,
        )
        finished.append(str(grant.task_id))
    return service.end_task_grants(finished) if finished else []


def register_task_access_cleanup(agent: Any) -> None:
    """Clean up after each ending or released task (once per agent); the team lead also checks finished tasks once."""

    callback = getattr(agent, "_task_access_terminal_callback", None)
    if callback is None:

        def callback(task_id: str, state: ProgressState, _timestamp: str) -> None:
            _clean_up_ended_task(agent, task_id, state)

        agent._task_access_terminal_callback = callback
    agent.task_manager.add_task_terminal_callback(callback)
    released = getattr(agent, "_task_access_release_callback", None)
    if released is None:
        released = partial(_clean_up_released_task, agent)
        agent._task_access_release_callback = released
    agent.task_manager.add_task_release_callback(released)
    if bool(getattr(agent, "is_team_lead", False)) and not getattr(agent, "_finished_task_grants_checked", False):
        agent._finished_task_grants_checked = True
        try:
            service = runtime_access(agent).service
            if service is not None:
                end_grants_of_finished_tasks(service, agent.task_manager.task_archive_repository)
                runtime_requests(agent).close_requests_saved_before_work_ids()
        except Exception:  # noqa: BLE001 - startup continues; the grants stay listed until the next check
            logger.exception("Could not check task-bound access of finished tasks. event=task_grants_check_failed")


def _is_turn_output(event: BeforeToolCallEvent) -> bool:
    """Whether this call is how the model hands back its answer, not an action.

    Strands delivers a structured-output model's answer (the runtime's turn
    decision, ``ModelAgentTurnDecision`` and its variants) as a call to its own
    ``StructuredOutputTool``. It does nothing and has no catalog entry, so
    deciding it like a function tool refused every turn. Checked on the tool
    object Strands selected, never on the name the model sent.
    """

    return isinstance(getattr(event, "selected_tool", None), StructuredOutputTool)


class AccessDecisionHook:
    """Strands hook that decides every function tool call (see module docstring)."""

    name = "access_decision"

    def __init__(self, agent: Any) -> None:
        self.agent = agent
        if getattr(agent, "task_manager", None) is not None:
            register_task_access_cleanup(agent)

    def register_hooks(self, registry: HookRegistry, **_: Any) -> None:
        registry.add_callback(BeforeToolCallEvent, self.before_tool_call, order=ACCESS_HOOK_ORDER)

    async def before_tool_call(self, event: BeforeToolCallEvent) -> None:
        if event.cancel_tool or _is_turn_output(event):
            return
        if event.selected_tool is None:
            # The agent has no tool by this name, so nothing can run: Strands
            # answers "Unknown tool: <name>" (this hook runs last, so no later
            # hook can route the call to a real tool). Refusing it as access
            # would tell the model a missing tool needs access; a TeamLead that
            # called its delegate_task action as a tool concluded that
            # delegation itself was broken.
            logger.info(
                "The model called a tool this agent does not have. event=unknown_tool_called agent_id=%s tool=%s",
                getattr(self.agent, "id", None),
                event.tool_use.get("name"),
            )
            return
        function = str(event.tool_use.get("name") or "").strip()
        arguments = event.tool_use.get("input")
        try:
            decision = await asyncio.to_thread(
                partial(decide_call, operation_id=call_operation_id(self.agent, event)),
                self.agent,
                function,
                arguments if isinstance(arguments, Mapping) else None,
            )
        except Exception:  # noqa: BLE001 - a decision that cannot run denies
            logger.exception(
                "Access decision failed; refusing the tool call. event=access_decision_failed agent_id=%s tool=%s",
                getattr(self.agent, "id", None),
                function,
            )
            event.cancel_tool = json.dumps(
                {"status": "access_denied", "reason_code": "access_decision_failed",
                 "reason": "The access check could not run, so the call was refused."}
            )
            return
        if decision is not None and not decision.allowed:
            event.cancel_tool = refusal_text(decision)


__all__ = [
    "ACCESS_HOOK_ORDER",
    "ActingAgent",
    "acting_agent",
    "AccessDecisionHook",
    "authorization_enabled",
    "AuthorizationOff",
    "RuntimeAccess",
    "agent_principal",
    "call_operation_id",
    "decide_call",
    "decided_elsewhere",
    "refusal_text",
    "register_task_access_cleanup",
    "request_id_of",
    "runtime_access",
    "runtime_requests",
    "send_access_notice",
    "send_runtime_notice_once",
    "resolve_meta_identity",
    "task_access",
    "task_person",
    "verified_context_id",
    "verified_meta_person_id",
    "verified_task_context",
]
