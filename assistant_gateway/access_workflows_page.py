"""The workflow page: a person follows an agent's access workflow and decides its requests.

A workflow (``custom_agents.access_workflows``) is a view over access
requests. This page shows, for the person the workflow belongs to:

- each step with its linked requests and their **current** status, read from
  the access store; a waiting request links to its Inbox review
  (``?view=inbox#access_request=<id>``);
- requests from the task that matched no single step, listed separately;
- tool setup pages (sign-ins, account links, the team's sign-in app) on the
  steps that expect them, with how each page ended in plain words; they update
  by themselves when the person finishes a page
  (``custom_agents.access_workflow_setups``);
- setup steps done outside Universal Agents (``external_step``, such as
  creating a token on a service's site) with **Mark done**: only the person
  the workflow belongs to marks their own waiting step, through the gateway's
  setup service, which tells the waiting task (``mark_setup_done``);
- **Review Ready Requests**: the waiting requests the person can decide, with
  the Inbox's own choices (Approve or Deny for a staged change; Approve,
  Approve read only, Approve for this task only or Deny for access). Each
  decision goes through ``GatewayAccessInbox.decide``, exactly as in the Inbox,
  which also wakes the waiting agent. A denial closes its step.

Nothing here grants access by itself or trusts roles from the browser: the
access service decides who may decide each request.
"""

from __future__ import annotations

import asyncio
import builtins
import logging
from typing import Any

from access_control.decisions import HandDownRefused
from assistant_gateway.access_inbox import GatewayAccessInbox
from assistant_gateway.application.access_workflows import WorkflowDecisionsV2, WorkflowPageRefused
from custom_agents.access_workflow_setups import setup_review_label, setup_view
from custom_agents.access_workflows import AccessWorkflowService, review_label
from custom_messages.contracts.access_v2 import AccessRequestStatus, AccessReviewChoice
from custom_messages.contracts.access_workflow_v2 import AccessWorkflowV2, WorkflowEventV2
from storage.access_workflow_repository import AccessWorkflowRepository

logger = logging.getLogger(__name__)
EVENT_PAGE_SIZE = 20

TASK_ENDED = "The source task has ended. Open Chat to go over the remaining work before deciding anything here."


def create_access_workflow_page(config: Any, access_inbox: GatewayAccessInbox) -> GatewayAccessWorkflowPage:
    """Compose the page from the task store and the gateway's access Inbox."""

    settings = config.access_workflows
    return GatewayAccessWorkflowPage(
        service=AccessWorkflowService(
            AccessWorkflowRepository(config.manager_storage_settings),
            max_steps=settings.max_steps,
            diagnostic_limit=settings.diagnostic_event_limit,
        ),
        access_inbox=access_inbox,
        team_id=config.manager.team_id,
    )


class GatewayAccessWorkflowPage:
    def __init__(self, *, service: AccessWorkflowService, access_inbox: GatewayAccessInbox, team_id: str) -> None:
        self.service = service
        self.access_inbox = access_inbox
        self.team_id = team_id

    def _record(self, workflow_id: str, owner_id: str) -> AccessWorkflowV2:
        return self.service.require_owner(workflow_id, owner_id=owner_id, team_id=self.team_id)

    # -- reading ---------------------------------------------------------------

    def list(self, *, owner_id: str, offset: int = 0, include_terminal: bool = False) -> dict[str, Any]:
        records = self.service.repository.list_owned(
            owner_id=owner_id, team_id=self.team_id, offset=offset, include_terminal=include_terminal
        )
        total = self.service.repository.count_owned(
            owner_id=owner_id, team_id=self.team_id, include_terminal=include_terminal
        )
        return {
            "workflows": [
                {
                    "workflow_id": record.workflow_id,
                    "title": record.title,
                    "phase": record.phase.value,
                    "target_agent_type": record.target_agent_type,
                }
                for record in records
            ],
            "offset": offset,
            "has_more": offset + len(records) < total,
            "total": total,
        }

    def _with_review_url(self, item: dict[str, Any]) -> dict[str, Any]:
        if item.get("outcome") == "pending":
            return {**item, "review_url": self.access_inbox.review_url(item["request_id"])}
        return item

    def _read(self, workflow_id: str, owner_id: str, event_offset: int) -> dict[str, Any]:
        record = self._record(workflow_id, owner_id)
        evidence = {step["step_id"]: step for step in self.service.evidence(record)}
        projection = record.model_dump(mode="json")
        for step, saved in zip(projection["steps"], record.steps):
            source = evidence[step["step_id"]]
            step["requests"] = [self._with_review_url(item) for item in source["requests"]]
            step["setups"] = source["setups"]
            step["blocked_by_person"] = source["blocked_by_person"]
            step["reason_code"] = source["reason_code"]
            step["explanation"] = source["explanation"]
            step["review_status"] = review_label(source) or setup_review_label(saved)
        unassigned = [self._with_review_url(item) for item in self.service.unassigned_evidence(record)]
        waiting = [
            item["request_id"]
            for step in projection["steps"]
            if not step["blocked_by_person"]
            for item in step["requests"]
            if item["outcome"] == "pending"
        ] + [item["request_id"] for item in unassigned if item["outcome"] == "pending"]
        return {
            "workflow": projection,
            "requests_not_on_a_step": unassigned,
            "setups_not_on_a_step": [setup_view(link) for link in record.unassigned_setups],
            "waiting_request_count": len(waiting),
            "task_status": self.service.task_status(record),
            "record_digest": record.record_digest,
            "event_total": self.service.repository.event_count(workflow_id),
            "events": [
                event.model_dump(mode="json")
                for event in self.service.repository.events(workflow_id, offset=event_offset, limit=EVENT_PAGE_SIZE)
            ],
        }

    async def read(self, workflow_id: str, *, owner_id: str, event_offset: int = 0) -> dict[str, Any]:
        return await asyncio.to_thread(self._read, workflow_id, owner_id, max(0, event_offset))

    def diagnostics(self, workflow_id: str, *, owner_id: str) -> dict[str, Any]:
        return self.service.diagnostics(self._record(workflow_id, owner_id))

    async def mark_setup_done(self, workflow_id: str, *, owner_id: str, setup_request_id: str) -> dict[str, Any]:
        """Mark the person's waiting external setup step done (module docstring)."""

        record = await asyncio.to_thread(self._record, workflow_id, owner_id)
        links = [*(link for step in record.steps for link in step.setups), *record.unassigned_setups]
        link = next((item for item in links if item.setup_request_id == setup_request_id), None)
        if link is None or link.kind != "external_step" or link.status != "waiting":
            raise WorkflowPageRefused("This step is not waiting to be marked done. Refresh the page.")
        setups = self.access_inbox.tool_setups
        if setups is None or not await setups.external_step_done(setup_request_id, person_id=owner_id):
            raise WorkflowPageRefused("This step was already marked done or no longer waits. Refresh the page.")
        logger.info(
            "A person marked a setup step done. event=workflow_setup_marked_done workflow_id=%s setup_request_id=%s",
            workflow_id,
            setup_request_id,
        )
        return {"marked_done": setup_request_id}

    # -- deciding ----------------------------------------------------------------

    @staticmethod
    def _workflow_request_ids(record: AccessWorkflowV2) -> dict[str, str | None]:
        """Request id -> its step (None when it is not on a step); denied steps' requests excluded."""

        ids: dict[str, str | None] = {
            request_id: step.step_id
            for step in record.steps
            if not step.blocked_by_person
            for request_id in step.request_ids
        }
        ids.update({request_id: None for request_id in record.unassigned_request_ids if request_id not in ids})
        return ids

    def _ready_reviews(self, workflow_id: str, owner_id: str) -> builtins.list[dict[str, Any]]:
        record = self._record(workflow_id, owner_id)
        if self.service.task_status(record) is not None:
            raise WorkflowPageRefused(TASK_ENDED)
        person = self.access_inbox.person(owner_id)
        if person is None:
            raise HandDownRefused("You are not an active member of this team.")
        requests = self.access_inbox.requests()
        reviews = []
        for request_id, step_id in self._workflow_request_ids(record).items():
            request = requests.service.store.get_request(request_id)
            if (
                request is None
                or request.status is not AccessRequestStatus.WAITING
                or request.team_id != record.team_id
                or request.person_id != record.owner_person_id
            ):
                continue
            can_decide = requests.may_decide(person, request)
            view = self.access_inbox.review_view(requests, request, can_decide=can_decide)
            reviews.append({**view, "step_id": step_id, "review_url": self.access_inbox.review_url(request_id)})
        return reviews

    async def prepare(self, workflow_id: str, *, owner_id: str) -> dict[str, Any]:
        reviews = await asyncio.to_thread(self._ready_reviews, workflow_id, owner_id)
        return {"reviews": reviews}

    def _preflight(self, workflow_id: str, owner_id: str, selection: WorkflowDecisionsV2) -> dict[str, str | None]:
        """Check the whole selection before deciding any of it; returns request id -> step."""

        ready = {review["request_id"]: review for review in self._ready_reviews(workflow_id, owner_id)}
        ids = [item.request_id for item in selection.decisions]
        if len(set(ids)) != len(ids):
            raise WorkflowPageRefused("Choose each request once.")
        for item in selection.decisions:
            review = ready.get(item.request_id)
            if review is None:
                raise WorkflowPageRefused(
                    "A selected request is no longer waiting in this workflow. Refresh and review again."
                )
            if not review["can_decide"]:
                raise HandDownRefused("You cannot decide one of the selected requests; it waits for a team administrator.")
            if item.choice not in {choice["choice"] for choice in review["choices"]}:
                raise WorkflowPageRefused("Choose one of the decisions offered for each request.")
        return {request_id: ready[request_id]["step_id"] for request_id in ids}

    async def decide(self, workflow_id: str, *, owner_id: str, selection: WorkflowDecisionsV2) -> dict[str, Any]:
        """Decide each selected request through the access Inbox (see the module docstring).

        The whole selection is checked first; each decision is still checked
        again when made, so a request decided elsewhere meanwhile is reported
        as not saved rather than decided twice.
        """

        steps = await asyncio.to_thread(self._preflight, workflow_id, owner_id, selection)
        results: builtins.list[dict[str, Any]] = []
        for item in selection.decisions:
            try:
                review = await self.access_inbox.decide(owner_id, item.request_id, item.choice, item.note)
                saved = True
            except HandDownRefused as exc:
                logger.warning(
                    "A decision from the workflow page was not saved. event=workflow_page_decision_refused "
                    "workflow_id=%s request_id=%s reason=%s",
                    workflow_id,
                    item.request_id,
                    exc,
                )
                results.append({"request_id": item.request_id, "saved": False, "message": str(exc)})
                saved = False
            if saved:
                results.append({"request_id": item.request_id, "saved": True, "review": review})
                if item.choice == AccessReviewChoice.DENY.value and steps[item.request_id] is not None:
                    await asyncio.to_thread(self.service.record_denial, workflow_id, item.request_id)
            await asyncio.to_thread(
                self.service.repository.append_event,
                WorkflowEventV2(
                    workflow_id=workflow_id,
                    step_id=steps[item.request_id],
                    kind="decision",
                    code="decision_recorded" if saved else "decision_not_confirmed",
                    message="A person decided a request from the workflow page."
                    if saved
                    else "A decision from the workflow page could not be saved; the request keeps its status.",
                ),
            )
        return {"results": results, "all_saved": all(item["saved"] for item in results)}


__all__ = ["GatewayAccessWorkflowPage", "create_access_workflow_page"]
