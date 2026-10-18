"""The request and answer that get a scheduled run its signed task context.

Only the manager process holds the scheduler's signing key
(``ACCESS_SCHEDULER_SIGNING_KEY``). Scheduled workers claim their own due
schedules from the shared database, and a worker started from team
composition runs as its own process without that key. Such a worker asks the
manager for the run's signed context with its claim
(``ScheduledRunContextRequestV1``); the manager checks the claim against the
schedule row and answers with the context it signed for the schedule's owner,
or with the reason it refused (``ScheduledRunContextReplyV1``).

The request names no person, team role or roles on purpose: the manager takes
the owner only from the schedule row. The answer grants nothing by itself; the
signature is what every access check verifies.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, model_validator

from custom_messages.contracts.access_v2 import SignedTaskContextV1

__all__ = [
    "ScheduledRunContextRefusal",
    "ScheduledRunContextReplyV1",
    "ScheduledRunContextRequestV1",
]


class ScheduledRunContextRefusal(StrEnum):
    """Why the manager did not sign a scheduled run's context."""

    SIGNER_MISSING = "signer_missing"  # the manager has no scheduler signing key
    WRONG_TEAM = "wrong_team"  # the request is for another team
    SENDER_MISMATCH = "sender_mismatch"  # the sender is not the agent the request names
    SCHEDULE_NOT_FOUND = "schedule_not_found"
    CLAIM_NOT_HELD = "claim_not_held"  # the requesting agent does not hold this claim (or it expired)
    SCHEDULE_STOPPED = "schedule_stopped"  # paused, turned off or past its end after it was claimed
    SCHEDULED_JOB_MISMATCH = "scheduled_job_mismatch"  # the schedule the context would name differs from the row's
    HEARTBEAT_ITEM_NOT_FOUND = "heartbeat_item_not_found"  # no such item of this heartbeat schedule
    OWNER_INACTIVE = "owner_inactive"  # the schedule's person is disabled or removed
    CHECK_FAILED = "check_failed"  # reading the schedule or the team's people failed


class _RunContextContract(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)


class ScheduledRunContextRequestV1(_RunContextContract):
    """A worker's request for the signed context of a run it claimed."""

    schema_version: Literal[1] = 1
    message_class: Literal["ScheduledRunContextRequestV1"] = "ScheduledRunContextRequestV1"
    request_id: str = Field(min_length=1, max_length=240)
    team_id: str = Field(min_length=1, max_length=240)
    agent_id: str = Field(min_length=1, max_length=240, description="The agent that holds the claim.")
    schedule_id: str = Field(min_length=1, max_length=240, description="The claimed schedule row.")
    claim_token: str = Field(min_length=1, max_length=240)
    scheduled_job_id: str = Field(
        min_length=1,
        max_length=240,
        description="The schedule the signed context names; for a heartbeat item, the item id.",
    )
    conversation_id: Optional[str] = Field(default=None, max_length=240)
    heartbeat_item_id: Optional[str] = Field(
        default=None, max_length=240, description="Set when the run is one item of a heartbeat schedule."
    )

    def get_id(self) -> str:
        return self.request_id


class ScheduledRunContextReplyV1(_RunContextContract):
    """The manager's answer: the signed context, or why it refused."""

    schema_version: Literal[1] = 1
    message_class: Literal["ScheduledRunContextReplyV1"] = "ScheduledRunContextReplyV1"
    request_id: str = Field(min_length=1, max_length=240)
    team_id: str = Field(min_length=1, max_length=240)
    agent_id: str = Field(min_length=1, max_length=240, description="The agent that asked.")
    schedule_id: str = Field(min_length=1, max_length=240)
    task_context: Optional[SignedTaskContextV1] = None
    refusal: Optional[ScheduledRunContextRefusal] = None

    @model_validator(mode="after")
    def _context_or_refusal(self) -> "ScheduledRunContextReplyV1":
        if (self.task_context is None) == (self.refusal is None):
            raise ValueError("A scheduled run context reply carries either a signed context or a refusal.")
        return self

    def get_id(self) -> str:
        return self.request_id
