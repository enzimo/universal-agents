"""Tool setup pages on access workflow steps (owner's decision 2026-09-28).

When a request needs several setup pieces, ``set_up_tools`` publishes one
access workflow with a step per piece (``custom_agents.tool_setup_runs``). A
step names the setup pages it expects in ``tool_setup`` (tool id, setup kind,
owner, and for an external step its catalog ``step_id``; checked against the
tool catalog when the plan is published). This module keeps those steps up
to date from trusted code, the same way access requests are linked
(``custom_agents.access_workflows``):

Linking and results run through the hooks in
``custom_agents.access_workflow_setup_hooks``.

- **Linking.** When trusted code sends a ``ToolSetupRequestV1`` for a run,
  it is linked to the one step of the task's workflow, or of a workflow whose task
  carries the same verified signed task context, that expects that tool,
  kind and owner and is open (running or waiting) or pending with every
  prerequisite complete. Pending steps count because nobody marks a setup
  step running before its page opens. The step then
  waits for the person. A setup that matches no single step is kept on the
  workflow as "not on a step" and logged.
- **Results.** When the ``ToolSetupResultV1`` arrives (the page ended, or the
  person finished the same setup with ``/link`` or ``/auth ... team_client``),
  the linked setup gets its status and the step changes:
  ``completed`` → a plain line ("Signed in as exampleuser"), and a step that only
  expects setups is complete once every one of them is done;
  ``expired`` → still waiting ("The page expired; a new one can be offered"),
  failed after the third page;
  ``declined`` → blocked by the person, like a denial (the gateway refuses
  further pages for that tool in the task);
  ``unavailable`` → failed when only an administrator can help or no page can
  be offered any more, otherwise still waiting with the reason.

Linking grants nothing and never decides a setup; the gateway's results are
the source of every status.
"""

from __future__ import annotations

import logging
from typing import Any, Iterable

from access_catalog.catalog import ToolCatalog
from access_control.account_sharing import TeamAccountSharing, team_account_private_text
from custom_messages.contracts.access_v2 import AccountMode, LinkOwnerKind
from custom_messages.contracts.access_workflow_v2 import (
    MAX_STEP_REQUESTS,
    MAX_UNASSIGNED_REQUESTS,
    OPEN_STEP_STATES,
    AccessWorkflowV2,
    WorkflowEventV2,
    WorkflowSetupLinkV2,
    WorkflowStepState,
    WorkflowStepV2,
    WorkflowToolSetupV2,
    utc_now,
)
from custom_messages.contracts.credentials_v1 import (
    TOOL_SETUP_OFFER_LIMIT,
    TOOL_SETUP_SOURCE_PERSON,
    ToolSetupRequestV1,
    ToolSetupResultV1,
)
from secure_tools.auth_methods import tool_sign_in
from storage.access_workflow_repository import WorkflowConflict

logger = logging.getLogger(__name__)

SAVE_ATTEMPTS = 3
SETUP_WAITING = "waiting"
SETUP_COMPLETED = "completed"
SETUP_DECLINED = "declined"
SETUP_EXPIRED = "expired"
SETUP_UNAVAILABLE = "unavailable"
# Reasons after which no page for the setup can be offered in the task (gateway).
_FINAL_UNAVAILABLE_REASONS = frozenset(
    {"tool_setup_offer_limit", "tool_setup_declined_before", "tool_setup_kind_mismatch", "tool_setup_admin_required"}
)
_KIND_WORDS = {"sign_in": "sign-in", "account_link": "account link", "team_sign_in_app": "team's sign-in app"}


# -- checking a published plan ---------------------------------------------------


def _page_problem(item: WorkflowToolSetupV2, tool: Any, sharing: TeamAccountSharing) -> str | None:
    """A sign-in, team sign-in app or account link that does not fit the tool, or the team's sharing."""

    sign_in = tool_sign_in(item.tool_id)
    if item.kind == "sign_in" and sign_in is None:
        return f"{item.tool_id} has no personal sign-in; it uses an account link."
    if item.kind == "team_sign_in_app" and (sign_in is None or sign_in.team_client_command is None):
        return f"{item.tool_id} has no team sign-in app."
    if item.kind != "account_link":
        return None
    if sign_in is not None and (item.owner != "team" or not sign_in.team_link):
        return f"{item.tool_id} uses each person's own sign-in, not a linked account."
    if sign_in is None and tool.account is not AccountMode.LINKED_CREDENTIAL:
        return f"{item.tool_id} uses no linked account."
    if item.owner not in tool.link_owners:
        # As the link form refuses it (secure_tool_broker.account_links.linkable_tool).
        whose = "each person's own account" if item.owner == LinkOwnerKind.TEAM else "the team's account"
        return f"{tool.display_name} uses only {whose}."
    if item.owner == LinkOwnerKind.TEAM and not sharing.team_account_used(tool):
        # A private service's team account would never be used (access_control.account_sharing).
        return team_account_private_text(tool)
    return None


def _external_step_problem(item: WorkflowToolSetupV2, tool: Any) -> str | None:
    """An external step the tool's catalog setup data does not have, or with the wrong owner."""

    step = tool.setup.step(item.step_id or "") if tool.setup else None
    if step is None:
        return f"{item.tool_id} has no setup step {item.step_id!r} in the catalog."
    if (item.owner == "team") != (step.who == "administrator"):
        owner = "team" if step.who == "administrator" else "person"
        return f"Setup step {item.step_id} of {item.tool_id} is done by {step.who}; use owner {owner}."
    return None


def setup_problem(item: WorkflowToolSetupV2, catalog: ToolCatalog, sharing: TeamAccountSharing) -> str | None:
    """Why a step's expected setup does not fit the tool, or None."""

    tool = catalog.tools.get(item.tool_id)
    if tool is None:
        return f"{item.tool_id} is not in the tool catalog; find_tools lists the tools."
    if item.kind == "external_step":
        return _external_step_problem(item, tool)
    return _page_problem(item, tool, sharing)


def check_setup_expectations(steps: Iterable[Any], catalog: ToolCatalog, sharing: TeamAccountSharing) -> None:
    """Every setup a step expects fits its tool and the team's sharing (raises ``ValueError`` naming the step)."""

    for step in steps:
        for item in step.tool_setup:
            problem = setup_problem(item, catalog, sharing)
            if problem:
                raise ValueError(f"Step {step.step_id}: {problem}")


# -- plain words for the page and the agent ----------------------------------------


def setup_title(link: WorkflowSetupLinkV2) -> str:
    sign_in = tool_sign_in(link.tool_id)
    name = sign_in.display_name if sign_in else link.tool_id
    if link.kind == "external_step":
        return f"{name}: {link.step_title or link.step_id}"
    whose = " (the team's account)" if link.kind == "account_link" and link.owner == "team" else ""
    return f"{name} {_KIND_WORDS.get(link.kind, 'setup')}{whose}"


def setup_status_text(link: WorkflowSetupLinkV2) -> str:
    """How the setup page stands, in plain words."""

    if link.status == SETUP_COMPLETED:
        if link.kind == "sign_in":
            return f"Signed in as {link.account_label}" if link.account_label else "Signed in"
        return "Marked done" if link.kind == "external_step" else "Set up"
    if link.kind == "external_step" and link.status == SETUP_WAITING:
        return "Waiting for the person to do this and mark it done"
    if link.status == SETUP_DECLINED:
        return "Closed without finishing; it will not be asked for again"
    if link.status == SETUP_EXPIRED:
        return "The page expired" + ("; no new page will be offered" if link.attempt >= TOOL_SETUP_OFFER_LIMIT else "")
    if link.status == SETUP_UNAVAILABLE:
        return link.message or "The page could not be used"
    if link.attempt > 1:
        return "The last page expired; a new one was offered"
    return "Waiting for the person to finish the page"


def setup_view(link: WorkflowSetupLinkV2) -> dict[str, Any]:
    return {
        **link.model_dump(
            mode="json", include={"setup_request_id", "tool_id", "kind", "owner", "status", "attempt", "step_id"}
        ),
        "title": setup_title(link),
        "status_text": setup_status_text(link),
    }


def setup_review_label(step: WorkflowStepV2) -> str | None:
    """The step's status line when it has setup pages and nothing else to review."""

    latest = _latest_by_setup(step.setups)
    if not latest:
        return None
    waiting = [link for link in latest if link.status == SETUP_WAITING]
    if waiting:
        return f"Waiting for the person: {', '.join(setup_title(link) for link in waiting)}"
    return "; ".join(f"{setup_title(link)}: {setup_status_text(link)}" for link in latest)


def setup_explanation(step: WorkflowStepV2) -> tuple[str, str] | None:
    """A trusted code and plain explanation for a step with setup pages, or None."""

    latest = _latest_by_setup(step.setups)
    if any(link.status == SETUP_DECLINED for link in latest):
        return (
            "setup_declined",
            "The person closed the setup page without finishing. It will not be asked for again in this workflow.",
        )
    if any(link.status == SETUP_WAITING for link in latest):
        return ("waiting_for_setup", "Waiting for the person to finish the setup page in their chat.")
    return None


def _latest_by_setup(links: Iterable[WorkflowSetupLinkV2]) -> list[WorkflowSetupLinkV2]:
    """The newest page per tool and kind (an expired page may be offered again)."""

    latest: dict[tuple[str, str, str | None], WorkflowSetupLinkV2] = {}
    for link in links:
        latest[(link.tool_id, link.kind, link.step_id)] = link
    return list(latest.values())


def setups_done(step: WorkflowStepV2) -> bool:
    """Every setup the step expects has a completed page."""

    done = {
        (link.tool_id, link.kind, link.owner, link.step_id) for link in step.setups if link.status == SETUP_COMPLETED
    }
    return all((item.tool_id, item.kind, item.owner, item.step_id) in done for item in step.tool_setup)


# -- linking and results ------------------------------------------------------------


class WorkflowSetups:
    """Link setup pages to steps and apply their results (module docstring)."""

    def __init__(self, service: Any) -> None:
        # custom_agents.access_workflows.AccessWorkflowService (not imported: it imports this module).
        self._service = service

    def _workflows(self, *, task_id: str, team_id: str, person_id: str, context_id: str | None) -> list[AccessWorkflowV2]:
        repository = self._service.repository
        records = {r.workflow_id: r for r in repository.for_task(task_id=task_id, team_id=team_id)}
        if context_id:
            for record in repository.for_context(context_id=context_id, team_id=team_id):
                records.setdefault(record.workflow_id, record)
        return [record for record in records.values() if record.owner_person_id == person_id]

    def link(
        self,
        request: ToolSetupRequestV1,
        *,
        team_id: str,
        person_id: str,
        context_id: str | None,
        step_title: str = "",
    ) -> None:
        """Show a setup page trusted code opened on its step (``step_title``: an external step's words)."""

        for record in self._workflows(task_id=request.parent_task_id, team_id=team_id, person_id=person_id, context_id=context_id):
            self._retry(
                lambda workflow_id=record.workflow_id: self._link_once(workflow_id, request, step_title),
                record.workflow_id,
            )

    def record_result(self, result: ToolSetupResultV1, *, team_id: str, person_id: str, context_id: str | None) -> None:
        """Apply a setup page's result to the step it is linked to."""

        for record in self._workflows(task_id=result.parent_task_id, team_id=team_id, person_id=person_id, context_id=context_id):
            if record.has_setup(result.setup_request_id):
                self._retry(lambda workflow_id=record.workflow_id: self._result_once(workflow_id, result), record.workflow_id)

    @staticmethod
    def _retry(action: Any, workflow_id: str) -> None:
        for attempt in range(SAVE_ATTEMPTS):
            try:
                action()
                return
            except WorkflowConflict:
                if attempt == SAVE_ATTEMPTS - 1:
                    raise
                logger.warning(
                    "Workflow changed while a setup page was recorded; retrying. event=workflow_setup_retry "
                    "workflow_id=%s",
                    workflow_id,
                )

    def _candidates(self, record: AccessWorkflowV2, request: ToolSetupRequestV1) -> list[WorkflowStepV2]:
        return [
            step
            for step in record.steps
            if not step.blocked_by_person
            and step.covers_setup(request.tool_id, request.kind, request.owner, request.step_id)
            and (
                step.state in OPEN_STEP_STATES
                or (step.state is WorkflowStepState.PENDING and not record.incomplete_dependencies(step))
            )
        ]

    def _link_once(self, workflow_id: str, request: ToolSetupRequestV1, step_title: str = "") -> None:
        record = self._service.repository.get(workflow_id)
        if record is None or record.terminal or record.has_setup(request.setup_request_id):
            return
        candidates = self._candidates(record, request)
        link = WorkflowSetupLinkV2(
            setup_request_id=request.setup_request_id,
            tool_id=request.tool_id,
            kind=request.kind,
            owner=request.owner,
            from_task_id=request.parent_task_id,
            step_id=request.step_id,
            step_title=step_title[:120],
        )
        if len(candidates) != 1 or len(candidates[0].setups) >= MAX_STEP_REQUESTS:
            self._keep_unassigned(record, link, candidates=len(candidates))
            return
        step = candidates[0]
        earlier = [
            item for item in step.setups if (item.tool_id, item.kind, item.step_id) == (link.tool_id, link.kind, link.step_id)
        ]
        link = link.model_copy(update={"attempt": len(earlier) + 1})
        changed = step.model_copy(
            update={"setups": (*step.setups, link), "state": WorkflowStepState.WAITING, "updated_at_utc": utc_now()}
        )
        steps = tuple(changed if item.step_id == step.step_id else item for item in record.steps)
        self._service._save(
            record,
            steps=steps,
            phase=self._service._phase(steps),
            event=WorkflowEventV2(
                workflow_id=record.workflow_id,
                step_id=step.step_id,
                kind="progress",
                code="setup_waiting",
                message=f"The setup page for {setup_title(link)} waits for the person.",
            ),
        )

    def _keep_unassigned(self, record: AccessWorkflowV2, link: WorkflowSetupLinkV2, *, candidates: int) -> None:
        logger.warning(
            "A tool setup page matched no single workflow step; it is shown as not on a step. "
            "event=workflow_setup_unassigned workflow_id=%s setup_request_id=%s tool_id=%s kind=%s candidates=%s",
            record.workflow_id,
            link.setup_request_id,
            link.tool_id,
            link.kind,
            candidates,
        )
        why = "several steps expect it" if candidates > 1 else "no open step expects it"
        self._service._save(
            record,
            unassigned=None,
            unassigned_setups=(*record.unassigned_setups, link)[-MAX_UNASSIGNED_REQUESTS:],
            event=WorkflowEventV2(
                workflow_id=record.workflow_id,
                kind="error",
                code="setup_not_on_step",
                message=f"The setup page for {setup_title(link)} is not shown on a step: {why}.",
            ),
        )

    def _result_once(self, workflow_id: str, result: ToolSetupResultV1) -> None:
        record = self._service.repository.get(workflow_id)
        if record is None or record.terminal:
            return
        step = next((item for item in record.steps if _has_link(item.setups, result.setup_request_id)), None)
        if step is None:
            unassigned = _with_result(record.unassigned_setups, result)
            self._service._save(record, unassigned_setups=unassigned, event=_result_event(record, None, result))
            return
        setups = _with_result(step.setups, result)
        changed = step.model_copy(update={"setups": setups, **_step_update(step, setups, result), "updated_at_utc": utc_now()})
        steps = tuple(changed if item.step_id == step.step_id else item for item in record.steps)
        self._service._save(
            record, steps=steps, phase=self._service._phase(steps), event=_result_event(record, step, result)
        )


def _has_link(links: Iterable[WorkflowSetupLinkV2], setup_request_id: str) -> bool:
    return any(link.setup_request_id == setup_request_id for link in links)


def _with_result(links: tuple[WorkflowSetupLinkV2, ...], result: ToolSetupResultV1) -> tuple[WorkflowSetupLinkV2, ...]:
    """The links with the result applied to the one it answers."""

    update = {
        "status": result.status,
        "attempt": max(result.attempt, 1),
        "account_label": result.account_label,
        "message": result.message[:2000],
        "updated_at_utc": utc_now(),
    }
    return tuple(
        link.model_copy(update=update) if link.setup_request_id == result.setup_request_id else link for link in links
    )


def _no_more_pages(result: ToolSetupResultV1) -> bool:
    """No page for this setup can be offered in the task any more (the step fails)."""

    if result.status == SETUP_EXPIRED:
        return result.attempt >= TOOL_SETUP_OFFER_LIMIT
    return result.status == SETUP_UNAVAILABLE and (
        result.needs_administrator or (result.reason_code or "") in _FINAL_UNAVAILABLE_REASONS
    )


def _step_update(step: WorkflowStepV2, setups: tuple[WorkflowSetupLinkV2, ...], result: ToolSetupResultV1) -> dict[str, Any]:
    """The step's new state and lines for one setup result (module docstring)."""

    link = next(item for item in setups if item.setup_request_id == result.setup_request_id)
    line = f"{setup_title(link)}: {setup_status_text(link)}."
    if result.status == SETUP_COMPLETED:
        finished = step.model_copy(update={"setups": setups})
        pure_setup = not (step.tool_access or step.change_kinds)
        state = WorkflowStepState.COMPLETE if pure_setup and setups_done(finished) else WorkflowStepState.RUNNING
        return {"state": state, "summary": line, "recovery": ""}
    if result.status == SETUP_DECLINED:
        return {
            "state": WorkflowStepState.OMITTED if step.optional else WorkflowStepState.FAILED,
            "blocked_by_person": True,
            "summary": line,
            "recovery": "This setup will not be asked for again in this workflow. Agree a different plan with the person.",
        }
    if _no_more_pages(result):
        return {
            "state": WorkflowStepState.FAILED,
            "summary": line,
            "recovery": f"The person can set it up later with /link {link.tool_id}, or a team administrator helps.",
        }
    return {"state": WorkflowStepState.WAITING, "summary": line, "recovery": ""}


def _result_event(record: AccessWorkflowV2, step: WorkflowStepV2 | None, result: ToolSetupResultV1) -> WorkflowEventV2:
    return WorkflowEventV2(
        workflow_id=record.workflow_id,
        step_id=step.step_id if step else None,
        kind="decision" if result.status == SETUP_DECLINED else "progress",
        code=f"setup_{result.status}",
        message=f"A setup page ended: {result.status}"
        + (" (the person did it on their own)." if result.source == TOOL_SETUP_SOURCE_PERSON else "."),
    )


__all__ = [
    "WorkflowSetups",
    "check_setup_expectations",
    "setup_explanation",
    "setup_problem",
    "setup_review_label",
    "setup_status_text",
    "setup_title",
    "setup_view",
    "setups_done",
]
