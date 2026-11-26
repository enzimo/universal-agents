"""Exact staged changes approved by people (access plan, Phase 4c).

Platform services stage exact, digest-checked plans (runtime settings,
restarts, new specialists, skills, hosted MCP servers). An agent
never applies one on its own authority when it lacks the change's level;
guardrail ``guard:agents-no-platform-full`` makes that always true for
platform changes. Instead:

1. the service describes the stored plan (``ChangeProposalV1``: kind, id,
   digest, title, exact effect lines, and a person who owns what it affects);
2. ``AccessRequests.submit_change`` applies at once only when the agent and
   its person already hold the change's level (possible for team-owned
   changes, such as team skills an administrator let the lead manage, and
   administrator-only ones, such as a model choice for an administrator's
   work, ``custom_agents.model_choice``); otherwise a person reviews the
   exact effect in the Inbox;
3. after approval, trusted code in the agent's process re-checks the stored
   request's change id, plan digest, owner and change kind against a fresh
   description, claims the approval once, and applies the plan
   (``ChangeApproval``). When the same change's digest, owner or kind no
   longer matches (for example after an upgrade that tightened who may
   approve it, or if the plan file changed), trusted code uses up the
   approval and records why so a stale approval cannot be retried or reused.
   If a request belongs to a different change id, it is refused without
   using up that other change's approval.

With authorization off there is no review: the person's conversational
agreement applies the change (decision D8).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Protocol

from custom_messages.contracts.access_v2 import ChangeApprovalMode, ChangeApprovalRecordV1, ChangeProposalV1


@dataclass(frozen=True)
class ChangeApproval:
    """Why trusted code may apply a staged change now; recorded with the result."""

    mode: ChangeApprovalMode
    approved_by: str  # a person id, the agent type that held access, or "agreement"
    approved_at_utc: datetime
    request_id: str | None = None  # the access request a person approved
    # The approver's note from the Inbox, returned to the agent with the result.
    # Not part of ``to_record``: configuration keeps who approved, not why.
    note: str = ""
    # The exact staged plan digest that was described or approved; checked by
    # services on apply so a plan swapped after describe/approval cannot apply.
    # Not part of ``to_record``: saved receipts keep plan_digest on their own.
    plan_digest: str | None = None

    def to_record(self) -> ChangeApprovalRecordV1:
        """The approval as saved with the configuration it created."""

        return ChangeApprovalRecordV1(
            mode=self.mode,
            approved_by=self.approved_by,
            approved_at_utc=self.approved_at_utc,
            access_request_id=self.request_id,
        )

    def record(self) -> dict[str, Any]:
        """The approval as JSON, for receipts and the supervisor's restart requests."""

        return self.to_record().model_dump(mode="json")


class ChangeAdapter(Protocol):
    """One kind of staged change, implemented by the service that stages it."""

    kind: str  # the catalog change kind (functions.yaml ``changes:``)

    def owns(self, change_id: str) -> bool:
        """Whether this adapter stages changes with this id."""

    def describe(self, agent: Any, change_id: str) -> ChangeProposalV1:
        """Load and check the stored plan (exists, unchanged, unexpired, same actor)."""

    def apply(self, agent: Any, change_id: str, approval: ChangeApproval) -> dict[str, Any]:
        """Apply the plan; only trusted code with an approval calls this.

        ``approval.plan_digest`` is the exact plan digest that was described or
        approved (across person approval, held access, and agreement). A service
        that reloads its plan from disk compares ``approval.plan_digest`` with
        the loaded plan's digest and refuses when it is missing or differs;
        ``RuntimeSettingsService`` enforces this check.

        An adapter may also define ``async after_apply(agent, result)`` for
        follow-up work that must not undo the change (refreshing running
        agents, notices); its result replaces the apply result.
        """


__all__ = ["ChangeAdapter", "ChangeApproval"]
