"""What the web layer needs from the access workflow page (``assistant_gateway.access_workflows_page``).

The person id always comes from the signed-in browser session. Decisions go
through the access Inbox, so deciding on the workflow page is the same as
deciding in the Inbox.
"""

from __future__ import annotations

from typing import Any, Literal, Protocol

from pydantic import Field

from custom_messages.contracts.access_workflow_v2 import WorkflowModel

# The access Inbox's review choices (``AccessReviewChoice``); a staged change
# is approved as it is or denied.
WorkflowChoice = Literal["approve", "approve_read", "approve_task", "deny"]


class WorkflowPageRefused(ValueError):
    """A refusal written for the person using the page; the web layer shows its text as is."""


class WorkflowDecisionV2(WorkflowModel):
    request_id: str = Field(min_length=1, max_length=240)
    choice: WorkflowChoice
    note: str = Field(default="", max_length=2000)


class WorkflowDecisionsV2(WorkflowModel):
    decisions: tuple[WorkflowDecisionV2, ...] = Field(min_length=1, max_length=20)


class AccessWorkflowApplication(Protocol):
    def list(self, *, owner_id: str, offset: int = 0, include_terminal: bool = False) -> dict[str, Any]:
        """The person's workflows, 20 per page."""

    async def read(self, workflow_id: str, *, owner_id: str, event_offset: int = 0) -> dict[str, Any]:
        """The workflow with each step's requests and their current status."""

    async def prepare(self, workflow_id: str, *, owner_id: str) -> dict[str, Any]:
        """The workflow's waiting requests this person can decide now, with their choices."""

    async def decide(self, workflow_id: str, *, owner_id: str, selection: WorkflowDecisionsV2) -> dict[str, Any]:
        """Decide several of the workflow's waiting requests through the access Inbox."""

    def diagnostics(self, workflow_id: str, *, owner_id: str) -> dict[str, Any]:
        """Structural diagnostics for download, without chat, request reasons or credentials."""

    async def mark_setup_done(self, workflow_id: str, *, owner_id: str, setup_request_id: str) -> dict[str, Any]:
        """The person did a setup step outside Universal Agents; the waiting task is told."""


__all__ = [
    "AccessWorkflowApplication",
    "WorkflowChoice",
    "WorkflowDecisionV2",
    "WorkflowDecisionsV2",
    "WorkflowPageRefused",
]
