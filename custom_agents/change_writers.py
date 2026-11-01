"""Who an agent's change is for, from verified identity only, for the change ledger (``storage.change_ledger``).

Every store the ledger covers asks here, so an agent's writer is worked out
the same way everywhere: from the active task's signed context or schedule
run identity (``custom_agents.access_runtime.verified_task_context``), never
from fields in a message or tool call.

- A context the gateway signed for a person is that person's request; its
  context id is the request id (``AgentPrincipal.work_id``).
- Any other context for a person (signed by the scheduler) is scheduled work.
- No valid context is work for nobody: system upkeep, which never counts
  against a request.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from access_control.identity import ISSUER_GATEWAY
from storage.change_ledger import ChangeWriter, work_writer


@dataclass(frozen=True)
class VerifiedWork:
    """Whom the agent's active task works for, checked."""

    person_id: str  # empty for work for nobody
    work_id: str  # the signed context id; empty without a valid context
    request_id: str  # the work id, only for a person's request
    conflict_checks: bool  # the operator's agent.request_conflict_checks switch, as this agent read it

    def change_writer(self) -> ChangeWriter:
        return work_writer(person_id=self.person_id, request_id=self.request_id, checked=self.conflict_checks)


def verified_work(agent: Any) -> VerifiedWork:
    """The active task's verified work; without a valid context, work for nobody."""

    from custom_agents.access_runtime import verified_task_context

    context = verified_task_context(agent)
    person_id = str(getattr(context, "person_id", "") or "") if context is not None else ""
    work_id = str(getattr(context, "context_id", "") or "") if context is not None else ""
    is_request = context is not None and context.issuer == ISSUER_GATEWAY and bool(person_id)
    return VerifiedWork(
        person_id=person_id,
        work_id=work_id,
        request_id=work_id if is_request else "",
        conflict_checks=bool(getattr(agent, "_request_conflict_checks", True)),
    )


def agent_change_writer(agent: Any) -> ChangeWriter:
    """The change ledger writer for the agent's active task."""

    return verified_work(agent).change_writer()


__all__ = ["VerifiedWork", "agent_change_writer", "verified_work"]
