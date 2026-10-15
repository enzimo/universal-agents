"""activity v1 contracts, independent of runtime hosts and SDKs."""

from custom_messages.enums import TaskActivityStage
from custom_messages.enums import TaskActivityType
from datetime import datetime
from datetime import timezone
from id_generation import new_id
from pydantic import BaseModel
from pydantic import Field
from typing import Any
from typing import Dict
from typing import Literal
from typing import Optional


class TaskActivityEvent(BaseModel):
    """
    Curated runtime activity event intended for collector ingestion and user/admin dashboards.

    This is an application-level observability contract and is not part of the
    inter-agent NATS work protocol.
    """

    activity_event_id: str = Field(
        default_factory=lambda: new_id("activity_evt"),
        description="Unique identifier for this activity event.",
    )
    message_class: Literal["TaskActivityEvent"] = Field(
        default="TaskActivityEvent",
        description="Task Activity Event",
    )
    created_at: str = Field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat(),
        description="ISO 8601 UTC timestamp when the activity event was created.",
    )
    sequence: int = Field(
        default=0,
        description="Best-effort per-process monotonic sequence value used to order activity for one agent.",
    )
    team_id: Optional[str] = Field(
        default=None, description="Team associated with the event."
    )
    manager_agent_id: Optional[str] = Field(
        default=None, description="Manager agent id for the team when known."
    )
    manager_name: Optional[str] = Field(
        default=None, description="Human-readable manager name when known."
    )
    top_level_task_id: Optional[str] = Field(
        default=None, description="Top-level task id for this activity lineage."
    )
    task_id: Optional[str] = Field(
        default=None, description="Task id directly associated with this event."
    )
    parent_task_id: Optional[str] = Field(
        default=None, description="Optional parent task id for delegated work."
    )
    objective_id: Optional[str] = Field(
        default=None,
        description="Durable Objective id associated with this activity, when any.",
    )
    objective_title: Optional[str] = Field(
        default=None,
        description="Human-readable Objective title associated with this activity, when any.",
    )
    correlation_id: Optional[str] = Field(
        default=None, description="Request-local correlation id when available."
    )
    conversation_id: Optional[str] = Field(
        default=None, description="Durable conversation id when available."
    )
    session_key: Optional[str] = Field(
        default=None, description="Gateway session key when available."
    )
    agent_id: Optional[str] = Field(
        default=None, description="Agent id that emitted the event."
    )
    agent_name: Optional[str] = Field(
        default=None, description="Agent name that emitted the event."
    )
    named_agent_type: Optional[str] = Field(
        default=None, description="Agent type that emitted the event."
    )
    activity_type: TaskActivityType = Field(description="Curated activity category.")
    stage: TaskActivityStage = Field(
        description="Current lifecycle stage for this activity."
    )
    summary: str = Field(description="Short user/admin-visible activity summary.")
    detail: Optional[str] = Field(
        default=None, description="Optional longer activity detail."
    )
    user_visible: bool = Field(
        default=True,
        description="Whether this event is safe to render in user-facing views.",
    )
    trace_id: Optional[str] = Field(
        default=None, description="OpenTelemetry trace id when available."
    )
    span_id: Optional[str] = Field(
        default=None, description="OpenTelemetry span id when available."
    )
    details: Dict[str, Any] = Field(
        default_factory=dict, description="Additional structured event details."
    )

    def get_id(self) -> str:
        return self.activity_event_id
