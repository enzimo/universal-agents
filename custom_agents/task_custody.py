"""Whether an agent may take a delivered task that another agent may already hold.

Why (2026-10-17): JetStream delivers a task again when the acknowledgement
of the first delivery does not arrive within the wait: the process that took
it stopped after saving the task but before acknowledging it, or NATS was
unreachable for longer than the wait. Each agent checked only its own records
for duplicates, so another worker of the type ran the copy while the first
holder, or the replacement that took over the first holder's saved work, ran
it as well. Every task an agent executes now has one team-wide custody row
(``storage.task_custody_repository``), claimed in the transaction that saves
the task.

An agent receiving a task that someone else holds:

- drops it (acknowledged, logged) while the holder is alive: the holder runs it;
- takes it over when the lead re-sent it because that holder handed it back
  after losing its launch lease (``redispatch_notice``), even though the holder
  is still finishing other work;
- takes it over when the holder is gone: if the holder's saved work still has
  the task, the replacement that takes that work over leaves it out
  (``RuntimeSnapshotCoordinator._taken_by_other_agents``), so that replacement
  does not run it too.

The takeover is a compare-and-set on the judged-gone holder in the
admission's save, so a racing recovery or a second copy cannot both win; the
loser drops its copy. The same transaction reads the finished-task archive.
A holder that released or finished the task meanwhile was alive after all: a
copy the lead re-sent after that holder's handback is admitted as a free
task; any other copy is dropped, and no fresh row is claimed for it
(``task_message_runtime``). A holder wrongly judged gone that is still
running finds the task taken at its next save and drops it
(``custom_agents.custody_loss``).

What this does not cover: work a holder judged gone did before it stopped,
or, if it is still running, until its next save (the taker starts the task
again); agents that do not share the team's database; and, with
``agent.same_role_runtime_takeover_enabled`` off, a copy dropped while its
holder looked alive when that holder stops later (nobody adopts the holder's
saved work).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime
from functools import partial
from typing import Any, Optional

from custom_agents.agent_liveness import LivenessPurpose, owner_liveness

logger = logging.getLogger(__name__)

# Why admission went the way it did (logged as ``reason``).
CUSTODY_FREE = "free"
CUSTODY_MINE = "already_mine"
CUSTODY_HANDED_BACK = "handed_back_by_holder"
CUSTODY_HOLDER_GONE = "holder_gone"
CUSTODY_HELD = "held_by_live_agent"
CUSTODY_TAKEN_MEANWHILE = "taken_by_another_agent_meanwhile"
# The holder a takeover named released the task before the takeover's save.
CUSTODY_RELEASED_MEANWHILE = "released_by_holder_meanwhile"


@dataclass(frozen=True)
class CustodyDecision:
    admit: bool
    reason: str
    # The current holder: the one to replace when admitting, or the one keeping it.
    holder_agent_id: Optional[str] = None

    @property
    def takeover_from(self) -> Optional[str]:
        """The holder admission replaces (compare-and-set), or None for a free task."""
        return self.holder_agent_id if self.admit and self.reason in {CUSTODY_HANDED_BACK, CUSTODY_HOLDER_GONE} else None


def holder_gone(agent: Any, holder_agent_id: str, *, now: Optional[datetime] = None) -> bool:
    """Whether a task's holder has provably stopped: the team's owner-liveness rule.

    One seam (``agent_liveness.owner_liveness``, logged with purpose
    ``custody_admission``): the holder's launch record decides when it has
    one, otherwise its heartbeat and process identity. Under that rule a live
    process counts as alive even after it announced its exit.
    """

    return owner_liveness(agent, holder_agent_id, purpose=LivenessPurpose.CUSTODY_ADMISSION, now=now).gone


def decide_admission(agent: Any, task_id: str, prior_worker_agent_id: Optional[str]) -> CustodyDecision:
    """Decide whether this agent may take ``task_id`` (storage reads; run on the task lane)."""

    team_id = str(agent.team_id or "")
    if not team_id:
        return CustodyDecision(True, CUSTODY_FREE)
    row = agent.task_manager.runtime_state_store.get_task_custody(team_id, [task_id]).get(task_id)
    if row is None:
        return CustodyDecision(True, CUSTODY_FREE)
    holder = row.holder_agent_id
    if holder == agent.id:
        return CustodyDecision(True, CUSTODY_MINE, holder)
    if prior_worker_agent_id and prior_worker_agent_id == holder:
        return CustodyDecision(True, CUSTODY_HANDED_BACK, holder)
    if holder_gone(agent, holder):
        return CustodyDecision(True, CUSTODY_HOLDER_GONE, holder)
    return CustodyDecision(False, CUSTODY_HELD, holder)


async def check_admission(agent: Any, task_id: str, msg_meta: Any) -> CustodyDecision:
    """Decide and log; a refused copy is acknowledged by the caller and dropped."""

    notice = getattr(msg_meta, "redispatch_notice", None)
    decision: CustodyDecision = await agent.task_manager.operations_runtime.run(
        partial(decide_admission, agent, task_id, getattr(notice, "prior_worker_agent_id", None))
    )
    if not decision.admit:
        log_dropped_copy(agent, task_id, decision.reason, decision.holder_agent_id)
    elif decision.takeover_from:
        agent._logger.warning(
            "Taking over a task another agent held. event=task_custody_takeover "
            "agent_id=%s task_id=%s previous_holder_agent_id=%s reason=%s",
            agent.id,
            task_id,
            decision.takeover_from,
            decision.reason,
        )
    return decision


def log_dropped_copy(agent: Any, task_id: str, reason: str, holder_agent_id: Optional[str]) -> None:
    agent._logger.warning(
        "Dropped a copy of a task another agent holds. event=task_copy_dropped_held_elsewhere "
        "agent_id=%s task_id=%s holder_agent_id=%s reason=%s",
        agent.id,
        task_id,
        holder_agent_id,
        reason,
    )


__all__ = [
    "CUSTODY_FREE",
    "CUSTODY_HANDED_BACK",
    "CUSTODY_HELD",
    "CUSTODY_HOLDER_GONE",
    "CUSTODY_MINE",
    "CUSTODY_RELEASED_MEANWHILE",
    "CUSTODY_TAKEN_MEANWHILE",
    "CustodyDecision",
    "check_admission",
    "decide_admission",
    "holder_gone",
    "log_dropped_copy",
]
