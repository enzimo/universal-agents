"""Operator-owned thresholds for the stuck-task check (``custom_agents.stuck_tasks``).

Each threshold is how long a task may be in that condition before the person it
works for (and administrators, when only they can fix it) are told.
"""

from typing import Any, Mapping

from pydantic import BaseModel, ConfigDict, Field


class StuckTaskSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enabled: bool = True
    # The agent holding the task stopped reporting in (crashed, or its
    # container was replaced) and no agent took the task over. It is also the
    # one heartbeat threshold of custom_agents.agent_liveness: an agent without
    # a start record that reports no process, or whose process cannot be
    # checked, counts as gone, for takeover and these notices, after this much
    # silence.
    owner_gone_after_seconds: float = Field(default=600, ge=60)
    # Waiting on an access decision or on the person's answer to a question.
    # Such waits may last two days, so this is a reminder, not a failure.
    waiting_on_person_after_seconds: float = Field(default=86400, ge=600)
    # In progress with no new history entry (a tool or service that never answers).
    no_progress_after_seconds: float = Field(default=7200, ge=600)
    # Handed back by a worker that lost its lease and sent out again, but no agent picked it up.
    handback_unclaimed_after_seconds: float = Field(default=1800, ge=60)

    @classmethod
    def from_schedule_maintenance(cls, schedule_maintenance: Mapping[str, Any] | None) -> "StuckTaskSettings":
        """The settings in an agent's ``schedule_maintenance`` mapping (defaults when absent)."""
        return cls.model_validate((schedule_maintenance or {}).get("stuck_tasks") or {})
