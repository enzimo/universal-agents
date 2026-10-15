"""Notice carried by a task that the TeamLead sends out again after a handback.

When a worker loses its launch lease mid-task, the task runs again on another
worker. The earlier run may already have sent messages or taken other actions,
so the re-dispatched request carries this notice in ``MessageMeta``. The
receiving worker turns it into a one-time runtime instruction for the task's
first turn: check what already happened before repeating anything.

This module imports nothing from the routing contracts so ``routing_v1`` can
reference it without an import cycle.
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

MAX_REDISPATCH_ACTION_NOTES = 50
MAX_REDISPATCH_TEXT = 500


class RedispatchContract(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, allow_inf_nan=False)


class DispatchedActionNoteV1(RedispatchContract):
    """One action an interrupted run had already sent before it was stopped."""

    action_id: str = Field(min_length=1, max_length=240)
    action_type: str = Field(min_length=1, max_length=120)
    status: str = Field(min_length=1, max_length=60)
    summary: str = Field(default="", max_length=MAX_REDISPATCH_TEXT)


class TaskRedispatchNoticeV1(RedispatchContract):
    """Why a task arrives again and what its interrupted run already did."""

    handback_id: str = Field(min_length=1, max_length=240)
    prior_disposition: Literal["not_started", "interrupted"]
    prior_worker_agent_id: str = Field(min_length=1, max_length=240)
    interruption_count: int = Field(default=0, ge=0)
    interrupted_at_utc: datetime
    dispatched_actions: tuple[DispatchedActionNoteV1, ...] = Field(
        default=(), max_length=MAX_REDISPATCH_ACTION_NOTES
    )


def render_redispatch_directive(notice: TaskRedispatchNoticeV1) -> str | None:
    """One-time model instruction for an interrupted task's next run, if any."""

    if notice.prior_disposition != "interrupted":
        return None
    lines = [
        "This task already ran on another worker, which lost its launch lease "
        f"and was stopped mid-run (interruption {notice.interruption_count}). "
        "Start the task again, but do not assume earlier work did or did not "
        "happen.",
    ]
    if notice.dispatched_actions:
        lines.append("Before it stopped, that run had already sent these actions:")
        lines.extend(
            f"- {note.action_type} ({note.status})"
            + (f": {note.summary}" if note.summary else "")
            for note in notice.dispatched_actions
        )
        lines.append(
            "Check whether each action above already took effect before repeating "
            "it. Never repeat an uncertain external action without checking its result."
        )
    return "\n".join(lines)
