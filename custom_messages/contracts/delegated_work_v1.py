"""Messages about delegated tasks after they leave the requester.

Delegated tasks wait in their worker type's shared queue until a free worker
takes one (ADR-019). ``DelegatedTaskClaimedV1`` is how the requester learns which
worker took a task, so it checks that exact worker and counts how often a task
had to move to another worker. ``TaskFailureV1`` says why a delegated task
failed in a form trusted code can act on, so nobody parses error text.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Literal, Optional

from pydantic import BaseModel, ConfigDict, Field


class DelegatedTaskClaimedV1(BaseModel):
    """A worker admitted a delegated task; sent to the task's requester.

    Sent after durable admission, and again by a worker that holds the task
    after a restart or took it over from a worker that is gone (``recovered``).
    Carries identifiers only; it grants nothing.
    """

    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)

    schema_version: Literal[1] = 1
    message_class: Literal["DelegatedTaskClaimedV1"] = "DelegatedTaskClaimedV1"
    claim_id: str = Field(min_length=1, max_length=240)
    team_id: str = Field(min_length=1, max_length=120)
    task_id: str = Field(min_length=1, max_length=240)
    parent_task_id: str = Field(min_length=1, max_length=240)
    worker_agent_id: str = Field(min_length=1, max_length=240)
    worker_agent_name: Optional[str] = Field(default=None, max_length=240)
    worker_named_agent_type: Optional[str] = Field(default=None, max_length=120)
    claimed_at_utc: datetime
    # The NATS delivery that brought the task (1 for the first); None when the
    # claim comes from recovery rather than a delivery.
    delivery_count: Optional[int] = Field(default=None, ge=1)
    recovered: bool = False

    def get_id(self) -> str:
        return self.claim_id


class TaskFailureCategory(StrEnum):
    """Why a delegated task failed, for trusted code on the requester's side."""

    # The model provider refused calls (rate limit, quota, overload) through
    # every retry, so the task could not be finished now.
    PROVIDER_LIMITED = "provider_limited"
    # Workers holding the task were lost more times than automatic retries allow.
    WORKER_LOST = "worker_lost"


class TaskFailureV1(BaseModel):
    """The typed reason a task failed; the error text stays for people."""

    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)

    category: TaskFailureCategory
    provider: Optional[str] = Field(default=None, max_length=120)
    model_id: Optional[str] = Field(default=None, max_length=240)
    status_code: Optional[int] = None
    attempts: Optional[int] = Field(default=None, ge=0)
