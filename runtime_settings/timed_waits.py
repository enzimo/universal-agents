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
longer without anyone's approval. They are one value for the whole team, so a
per-agent-type override (``agent_types.<type>.agent``) is refused in both
config.yaml and instance_config.yaml (``refuse_per_type_wait_limits``).
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, model_validator

from default_values import AGENT_DEFAULTS

# The longest a task may wait in total, even with a person's approval (24 hours).
WAIT_MINUTES_CEILING = 24 * 60
WITHOUT_APPROVAL_PATH = "agent.max_wait_minutes_without_approval"
WITH_APPROVAL_PATH = "agent.max_wait_minutes_with_approval"
# The keys under ``agent``, which no agent type may override.
WAIT_LIMIT_KEYS = tuple(path.rsplit(".", 1)[-1] for path in (WITHOUT_APPROVAL_PATH, WITH_APPROVAL_PATH))


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


def refuse_per_type_wait_limits(agent_overrides: Mapping[str, Any], agent_type: str = "<agent type>") -> None:
    """Refuse wait limits in one agent type's ``agent`` overrides, naming the key and the team-wide setting."""

    for key in WAIT_LIMIT_KEYS:
        if key in agent_overrides:
            raise ValueError(
                f"agent_types.{agent_type}.agent.{key} is not allowed: how long a task may wait is one limit for "
                f"the whole team. Set {WITHOUT_APPROVAL_PATH} and {WITH_APPROVAL_PATH} instead."
            )


__all__ = [
    "WAIT_LIMIT_KEYS",
    "WAIT_MINUTES_CEILING",
    "WITHOUT_APPROVAL_PATH",
    "WITH_APPROVAL_PATH",
    "TimedWaitLimits",
    "refuse_per_type_wait_limits",
]
