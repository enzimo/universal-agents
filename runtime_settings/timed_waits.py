"""How long an agent's task may pause itself with ``wait`` and ``long_wait``.

A task may pause for a set number of minutes and then continue
(``custom_agents.timed_wait_runtime``). All of a task's waits together are
limited: up to ``agent.max_wait_minutes_without_approval`` minutes need no
permission (``wait``), and up to ``agent.max_wait_minutes_with_approval``
minutes need a person's approval for ``long_wait`` through the access Inbox.
The approval limit can never exceed one day (``WAIT_MINUTES_CEILING``): a
paused turn stays in its agent's memory for the whole wait, and a longer
pause is better served by a schedule. Only the operator changes these limits
(``OPERATOR_ONLY_RUNTIME_SETTINGS``): raising the first would let tasks wait
longer without anyone's approval.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field, model_validator

from default_values import AGENT_DEFAULTS

# The longest a task may wait in total, even with a person's approval (24 hours).
WAIT_MINUTES_CEILING = 24 * 60
WITHOUT_APPROVAL_PATH = "agent.max_wait_minutes_without_approval"
WITH_APPROVAL_PATH = "agent.max_wait_minutes_with_approval"


class TimedWaitLimits(BaseModel):
    """A task's waiting-time limits in whole minutes, valid by construction."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    without_approval: int = Field(default=AGENT_DEFAULTS.max_wait_minutes_without_approval.value, ge=1)
    with_approval: int = Field(default=AGENT_DEFAULTS.max_wait_minutes_with_approval.value, ge=1)

    @model_validator(mode="after")
    def _ordered_and_bounded(self) -> TimedWaitLimits:
        if self.with_approval > WAIT_MINUTES_CEILING:
            raise ValueError(
                f"{WITH_APPROVAL_PATH} is {self.with_approval}, but a task may wait at most "
                f"{WAIT_MINUTES_CEILING} minutes (24 hours) in total."
            )
        if self.with_approval < self.without_approval:
            raise ValueError(
                f"{WITH_APPROVAL_PATH} ({self.with_approval}) must be at least "
                f"{WITHOUT_APPROVAL_PATH} ({self.without_approval}): a person's approval never "
                "allows less waiting than no approval."
            )
        return self


__all__ = ["WAIT_MINUTES_CEILING", "WITHOUT_APPROVAL_PATH", "WITH_APPROVAL_PATH", "TimedWaitLimits"]
