"""``set_up_tools``: TeamLead sets up the tools a request needs in one call (owner's decision 2026-09-28).

When a request needs a tool that acts with an account and TeamLead's context
does not list it as ready, TeamLead calls ``set_up_tools`` straight away,
never asking the person whether to start: the approval cards and setup pages
are where the person agrees. Trusted code does the whole routine:

1. checks each tool, and the tools its catalog ``setup`` data names as
   prerequisites (``custom_tools.tool_setup_check``);
2. plans the pieces (``custom_agents.tool_setup_runs``); a piece only an
   administrator can do, for a person who is not one, ends the call with a
   plain explanation before anything is asked;
3. publishes one access workflow when there are two or more pieces (or an
   external step the person marks done), with TeamLead's own extra steps and
   a final work step;
4. asks for TeamLead's missing access (``AccessRequests.submit``, one request
   per tool, found again by the tool call's id), stores the run, and pauses
   the task with a local Strands interrupt. ``custom_agents.tool_setup_run_runtime``
   then opens the setup pages, hands the worker its access, offers expired
   pages again, and resumes the task when everything is done or the person
   closed a page or denied a request;
5. returns a compact per-tool result: ready or not, and what happened.

The person is always the one in the task's signed context. Only the team
lead holds the tool (``fn:tool_setup`` in ``team_lead_runtime``), because only
it may ask the person; specialists escalate to it. Nothing here grants access.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from strands import tool
from strands.interrupt import InterruptException
from strands.types.tools import ToolContext

from access_control.identity import IdentityRejected
from access_control.people import is_administrator
from custom_messages.contracts.access_v2 import AccessLevel, AccessRequestStatus, TargetKind, TargetRefV1
from custom_tools.provider_contract import ToolProvider
from custom_tools.runtime_context import get_agent_ref

logger = logging.getLogger(__name__)

STATUS_REFUSED = "refused"


def _json(payload: dict[str, Any]) -> str:
    return json.dumps(payload, default=str, sort_keys=True)


def _names(catalog: Any, tool_ids: list[str]) -> dict[str, str]:
    from custom_agents.tool_setup_runs import service_name

    return {
        tool_id: service_name(tool_id, catalog.tools[tool_id].display_name if tool_id in catalog.tools else "")
        for tool_id in tool_ids
    }


def _ready_answer(reports: list[dict[str, Any]], names: dict[str, str], notes: list[str]) -> dict[str, Any]:
    tools = []
    for report in reports:
        account = ((report.get("sign_in") or {}).get("account")) or ((report.get("known_state") or {}).get("account_label"))
        tools.append({
            "tool_id": report["tool_id"],
            "name": names.get(report["tool_id"], report["tool_id"]),
            "ready": True,
            **({"account": account} if account else {}),
        })
    return {
        "status": "ready",
        "tools": tools,
        **({"notes": notes} if notes else {}),
        "next_step": "Everything is set up: do the work now.",
    }


def _stop_answer(stops: list[str]) -> dict[str, Any]:
    return {
        "status": "needs_administrator",
        "say": list(dict.fromkeys(stops)),
        "next_step": "Tell the person these lines in plain words and end the task. Nothing was asked or opened.",
    }


# -- starting a run -----------------------------------------------------------------------


class _Start:
    """One first call of the tool: check, plan, publish, ask, store (module docstring)."""

    def __init__(self, agent: Any, tool_context: ToolContext, *, level: str, worker: str, purpose: str) -> None:
        from custom_agents.access_runtime import runtime_access

        self.agent = agent
        self.access = runtime_access(agent)
        self.catalog = self.access.catalog
        self.tool_use_id = str(tool_context.tool_use.get("toolUseId") or "")
        if not self.tool_use_id:
            raise RuntimeError("This tool call has no id, so its setup cannot be found again.")
        self.level = level
        self.worker = worker
        self.purpose = purpose

    def run(self, tool_ids: list[str], extra_steps: list[dict[str, Any]]) -> Any:
        from custom_agents.tool_setup_run_runtime import complete_access_steps, run_repository
        from custom_agents.tool_setup_runs import ToolSetupRunV1, needs_workflow, plan_run, run_id_for, settle
        from custom_tools.tool_setup_check import check_tools

        context, reports, problem = check_tools(tool_ids, self.level, self.worker)
        person = context["person"]
        plan = plan_run(reports, self.catalog, is_admin=is_administrator(person.roles))
        names = _names(self.catalog, tool_ids)
        notes = plan.notes + ([problem] if problem else [])
        if plan.unknown:
            raise ValueError(
                f"{', '.join(plan.unknown)} is not in the tool catalog. find_tools lists tools; for a service the "
                "catalog lacks, propose it with propose_access_change."
            )
        if plan.stops:
            return _stop_answer(plan.stops)
        if not plan.pieces:
            return _ready_answer(reports, names, notes)
        run = ToolSetupRunV1(
            run_id=run_id_for(person.team_id, str(context["task_id"]), self.tool_use_id),
            team_id=person.team_id,
            person_id=person.person_id,
            person_roles=tuple(person.roles),
            task_id=str(context["task_id"]),
            agent_id=str(self.agent.id),
            agent_type=str(self.agent.named_agent_type),
            tool_ids=tuple(tool_ids),
            level=self.level,
            worker_type=self.worker,
            purpose=self.purpose,
            pieces=tuple(plan.pieces),
            notes=tuple(notes),
        )
        repository = run_repository(self.agent)
        run = repository.create(run)
        if needs_workflow(run.pieces):
            run = repository.save(self._publish(run, names, extra_steps))
        run = repository.save(settle(self._ask_for_access(run)))
        complete_access_steps(self.agent, run)
        logger.info(
            "Tool setup started. event=tool_setup_run_started run_id=%s task_id=%s tools=%s pieces=%s workflow_id=%s",
            run.run_id,
            run.task_id,
            ",".join(run.tool_ids),
            len(run.pieces),
            run.workflow_id or "-",
        )
        return run

    def _publish(self, run: Any, names: dict[str, str], extra_steps: list[dict[str, Any]]) -> Any:
        """One access workflow for the run (none when the task already has one)."""

        from custom_agents.access_runtime import verified_context_id
        from custom_agents.access_workflows import WorkflowRequester, workflow_service_for
        from custom_agents.tool_setup_runs import ACCESS_REQUESTED_CODE, PIECE_ACCESS, workflow_proposal
        from custom_messages.contracts.access_workflow_v2 import WorkflowProgressV2, WorkflowProposalV2
        from custom_tools.runtime_context import active_task_context

        service = workflow_service_for(self.agent)
        existing = service.repository.for_task(task_id=run.task_id, team_id=run.team_id)
        if existing:
            note = "This task already has a workflow; setup pages show on its steps that expect them."
            return run.model_copy(update={"workflow_id": existing[0].workflow_id, "notes": (*run.notes, note)})
        listed = ", ".join(names.get(tool_id, tool_id) for tool_id in run.tool_ids)
        proposal, run = workflow_proposal(
            run,
            names,
            title=f"Set up {listed}",
            goal=self.purpose or f"Set up {listed} for this request, then do the work.",
            target_agent_type=self.worker or run.agent_type,
            extra_steps=extra_steps,
        )
        _task, meta = active_task_context(self.agent)
        record = service.submit(
            WorkflowProposalV2.model_validate(proposal),
            requester=WorkflowRequester(
                agent_id=run.agent_id,
                agent_type=run.agent_type,
                team_id=run.team_id,
                person_id=run.person_id,
                context_id=verified_context_id(self.agent),
            ),
            task_id=run.task_id,
            idempotency_key=f"set_up_tools:{run.run_id}",
            catalog=self.catalog,
            conversation_id=getattr(meta, "conversation_id", None),
            correlation_id=getattr(meta, "msg_correlation_id", None),
            session_key=getattr(getattr(meta, "gateway_context", None), "session_key", None),
        )
        # Access steps run now, so the requests asked for next are shown on them.
        for step_id in dict.fromkeys(p.workflow_step_id for p in run.pieces if p.kind == PIECE_ACCESS):
            record = service.trusted_progress(
                service.repository.get(record.workflow_id) or record,
                WorkflowProgressV2(step_id=step_id, state="running", summary="Asking for access."),
                code=ACCESS_REQUESTED_CODE,
            )
        return run.model_copy(update={"workflow_id": record.workflow_id})

    def _ask_for_access(self, run: Any) -> Any:
        """One access request per tool TeamLead lacks, found again by the call's id."""

        from custom_agents.access_runtime import AuthorizationOff, runtime_access, runtime_requests, task_access
        from custom_agents.tool_setup_runs import DENIED, DONE, PIECE_ACCESS, WAITING
        from custom_tools.runtime_context import active_task_context

        try:
            requests = runtime_requests(self.agent)
        except AuthorizationOff:
            return run
        from access_control.entities import PersonPrincipal

        # With the request it works on (work_id), which task-only access is bound to.
        principal, _person, _task_id = task_access(self.agent, runtime_access(self.agent))
        person = PersonPrincipal(run.person_id, run.team_id, run.person_roles)
        _task, meta = active_task_context(self.agent)
        for piece in [item for item in run.pieces if item.kind == PIECE_ACCESS and item.request_id is None]:
            call_id = f"{self.tool_use_id}:access:{piece.tool_id}"
            request = requests.service.store.request_for_tool_use(
                team_id=run.team_id, agent_id=principal.agent_id, task_id=run.task_id, tool_use_id=call_id
            ) or requests.submit(
                requester=principal,
                person=person,
                target=TargetRefV1(kind=TargetKind.TOOL, id=piece.tool_id),
                level=AccessLevel(run.level),
                reason=f"To {self.purpose}"[:2000] if self.purpose else f"The request needs {piece.tool_id}.",
                task_id=run.task_id,
                conversation_id=getattr(meta, "conversation_id", None),
                tool_use_id=call_id,
                # A worker does the work: its tasks get the access from TeamLead.
                hand_on=bool(run.worker_type),
            ).request
            if request is None or request.status is AccessRequestStatus.GRANTED:
                changed = piece.model_copy(update={"status": DONE, "request_id": getattr(request, "request_id", None)})
            elif request.status is AccessRequestStatus.WAITING:
                changed = piece.model_copy(update={"status": WAITING, "request_id": request.request_id})
            else:
                changed = piece.model_copy(update={"status": DENIED, "request_id": request.request_id})
            run = run.with_piece(changed)
        return run


def _team_lead() -> Any:
    agent = get_agent_ref()
    if agent is None:
        raise RuntimeError("No agent is running this tool.")
    if not bool(getattr(agent, "is_team_lead", False)):
        raise PermissionError("Only the team lead sets tools up; escalate to TeamArchitect with the tool ids.")
    return agent


def _level(level: str) -> str:
    try:
        return AccessLevel(str(level or "read").strip().lower()).value
    except ValueError as exc:
        raise ValueError("level is read, write or full.") from exc


def _wait(agent: Any, run: Any, tool_context: ToolContext) -> Any:
    """Pause the turn until trusted code ends the run (the answer is a wake-up; the run is re-read)."""

    from custom_agents.tool_setup_run_runtime import run_repository
    from custom_agents.tool_setup_runs import TOOL_SETUP_RUN_KIND

    waiting = sum(1 for piece in run.pieces if piece.status in {"pending", "waiting"})
    tool_context.interrupt(
        TOOL_SETUP_RUN_KIND,
        reason={
            "kind": TOOL_SETUP_RUN_KIND,
            "run_id": run.run_id,
            "summary": f"Waiting for {waiting} setup step(s) for {', '.join(run.tool_ids)}.",
        },
    )
    return run_repository(agent).get(run.run_id) or run


def _set_up(
    tool_ids: list[str], tool_context: ToolContext, *, agent_type: str, purpose: str, level: str,
    extra_steps: list[dict[str, Any]],
) -> dict[str, Any]:
    from custom_agents.access_runtime import task_person
    from custom_agents.tool_setup_run_runtime import outcome_for, run_repository
    from custom_agents.tool_setup_runs import RUN_WAITING, run_id_for

    agent = _team_lead()
    start = _Start(agent, tool_context, level=_level(level), worker=str(agent_type or "").strip(), purpose=str(purpose or "").strip())
    wanted = list(dict.fromkeys(str(item or "").strip() for item in (tool_ids or []) if str(item or "").strip()))
    person, task_id = task_person(agent, start.access)
    if person is None or not task_id:
        raise IdentityRejected("This task works for no person, so there is nobody to set tools up for.")
    # A resumed call finds the run it started by the tool call's id.
    run = run_repository(agent).get(run_id_for(person.team_id, task_id, start.tool_use_id))
    if run is None:
        started = start.run(start.catalog.setup_order(wanted), list(extra_steps or []))
        if isinstance(started, dict):
            return started
        run = started
    if run.status == RUN_WAITING:
        run = _wait(agent, run, tool_context)
    return outcome_for(agent, run)


@tool(context=True)
def set_up_tools(
    tool_ids: list[str],
    tool_context: ToolContext,
    agent_type: str = "",
    purpose: str = "",
    level: str = "read",
    extra_steps: list[dict[str, Any]] | None = None,
) -> str:
    """Set up the tools a request needs for the person you work for, then do the work.

    Call it immediately when a request needs a tool that acts with an account
    (GitHub, Google, X, linked web API tools, MCP servers with credentials)
    and your context does not list it as ready, or a call failed for a setup
    reason. Never ask the person whether to start: the approval cards and
    setup pages are where they agree. A question about what you can access
    counts as a request to set up and answer.

    tool_ids: catalog tool ids (find_tools lists them). agent_type: the worker
    you will delegate to, if any. purpose: the request in a few plain words,
    such as "check your GitHub for new repos". level: read (default), write
    or full. extra_steps: optional workflow steps of your own (the shape of
    submit_access_workflow steps), shown before the final work step.

    Trusted code asks for missing access, opens every sign-in, account link
    and team sign-in app page in the person's chat (in order where one needs
    another), shows one workflow page when there are two or more things to
    do, offers an expired page again up to three times, and pauses your task
    until everything is done or the person closes a page or denies a request.
    It returns per tool whether it is ready and what happened. status
    needs_administrator: tell the person the say lines and end the task.
    not_ready: tell them plainly what is not set up; never ask again for
    something they closed or denied.
    """

    from custom_agents.access_runtime import AuthorizationOff
    from storage.access_workflow_repository import WorkflowConflict

    try:
        return _json(
            _set_up(
                tool_ids, tool_context, agent_type=agent_type, purpose=purpose, level=level,
                extra_steps=list(extra_steps or []),
            )
        )
    except InterruptException:
        raise
    except (AuthorizationOff, IdentityRejected, PermissionError, RuntimeError, ValueError, WorkflowConflict) as exc:
        logger.warning("set_up_tools was refused. event=tool_setup_refused error=%s", exc)
        return _json({"status": STATUS_REFUSED, "reason": str(exc)})


PROVIDER = ToolProvider("tool_setup", (set_up_tools,))

__all__ = ["PROVIDER", "set_up_tools"]
