"""Task handback from a worker runtime that lost its launch lease, or whose process drains.

A spawned worker renews a fenced launch lease from its scheduler loop. Once the
lease is lost, another owner may hold that launch, so the worker winds down: it
returns work it has not started and the task it was executing to the TeamLead,
keeps tasks that wait on a person or subtask until a deadline, then exits. A
worker whose process runs another message contract than its installation winds
down the same way (``custom_agents.contract_drain``), without any fault; the
evidence's ``cause`` tells the two apart, and only a lost lease counts toward the
interruption limit (``custom_agents.worker_interruption_escalation``).

Each message carries exactly one task so it stays small and so its identity is
deterministic per worker launch and task: a resend is a duplicate, not new
work. The message is evidence from a trusted runtime, never an authorization
grant; the TeamLead validates the sender against the stored launch record.
"""

from __future__ import annotations

import hashlib
from datetime import datetime
from typing import Literal, Optional

from pydantic import BaseModel, ConfigDict, Field

from custom_messages.contracts.redispatch_v1 import DispatchedActionNoteV1
from custom_messages.contracts.routing_v1 import MessageHeader, MessageMeta
from custom_messages.contracts.task_v1 import Task

HANDBACK_NOT_STARTED = "not_started"
HANDBACK_INTERRUPTED = "interrupted"
# Why a worker winds down and hands work back (``WorkerLeaseLossEvidenceV1.cause``).
WIND_DOWN_LEASE_LOST = "launch_lease_lost"
WIND_DOWN_OTHER_CONTRACT = "other_message_contract"
WindDownCause = Literal["launch_lease_lost", "other_message_contract"]
MAX_HANDBACK_ERROR_ENTRIES = 10
MAX_HANDBACK_ACTION_NOTES = 50
MAX_HANDBACK_TEXT = 500

__all__ = [
    "DispatchedActionNoteV1",
    "HandedBackTaskV1",
    "WIND_DOWN_LEASE_LOST",
    "WIND_DOWN_OTHER_CONTRACT",
    "WindDownCause",
    "WorkerLeaseLossEvidenceV1",
    "WorkerTaskHandbackV1",
    "handback_identity",
]


class HandbackContract(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, allow_inf_nan=False)


class WorkerLeaseLossEvidenceV1(HandbackContract):
    """Why the worker stopped: its launch identity and recent scheduler errors."""

    launch_id: Optional[str] = Field(default=None, max_length=240)
    lease_lost_at_utc: datetime
    lease_expires_at_utc: Optional[datetime] = None
    reason: str = Field(min_length=1, max_length=MAX_HANDBACK_TEXT)
    recent_scheduler_errors: tuple[str, ...] = Field(
        default=(), max_length=MAX_HANDBACK_ERROR_ENTRIES
    )
    # Why it winds down (module docstring). Handbacks saved before the cause was
    # recorded all came from a lost lease, the only cause there was.
    cause: WindDownCause = WIND_DOWN_LEASE_LOST


class HandedBackTaskV1(HandbackContract):
    """A task returned with its original request so it can run again unchanged."""

    task_id: str = Field(min_length=1, max_length=240)
    disposition: Literal["not_started", "interrupted"]
    prior_state: str = Field(min_length=1, max_length=60)
    task: Task
    header: MessageHeader
    meta: MessageMeta
    # The scheduled job this task ran for; the lead resolves the occurrence
    # receipt from the task id, so the worker needs no storage read here.
    schedule_id: Optional[str] = Field(default=None, max_length=240)
    dispatched_actions: tuple[DispatchedActionNoteV1, ...] = Field(
        default=(), max_length=MAX_HANDBACK_ACTION_NOTES
    )


class WorkerTaskHandbackV1(HandbackContract):
    """One task returned to the TeamLead by a worker that lost its lease."""

    schema_version: Literal[1] = 1
    message_class: Literal["WorkerTaskHandbackV1"] = "WorkerTaskHandbackV1"
    handback_id: str = Field(min_length=1, max_length=240)
    team_id: str = Field(min_length=1, max_length=240)
    worker_agent_id: str = Field(min_length=1, max_length=240)
    worker_named_agent_type: str = Field(min_length=1, max_length=240)
    evidence: WorkerLeaseLossEvidenceV1
    handed_back: HandedBackTaskV1


def handback_identity(*, worker_agent_id: str, launch_id: str | None, task_id: str) -> str:
    """Deterministic handback id: one handback per worker launch and task."""

    material = "\x1f".join((worker_agent_id, launch_id or "", task_id))
    return "handback_" + hashlib.sha256(material.encode("utf-8")).hexdigest()[:32]
