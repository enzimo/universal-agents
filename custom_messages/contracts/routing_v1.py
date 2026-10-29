"""routing v1 contracts, independent of runtime hosts and SDKs."""

from custom_messages.contracts.access_v2 import ScheduleRunIdentityV1
from custom_messages.contracts.access_v2 import SignedTaskContextV1
from custom_messages.contracts.redispatch_v1 import TaskRedispatchNoticeV1
from custom_messages.contracts.task_v1 import TaskQueryChoiceOption
from custom_messages.enums import RequesterType
from custom_messages.enums import RoutingType
from datetime import datetime
from datetime import timezone
from id_generation import new_id
from pydantic import BaseModel
from pydantic import ConfigDict
from pydantic import Field
from typing import Any
from typing import Dict
from typing import List
from typing import Literal
from typing import Optional
from typing import Union


class MessageHeader(BaseModel):
    """
    Contains standard routing and identification metadata for a message.

    Attributes:
        from_agent_id (Optional[str]):
            Optional identifier of the agent originating the message. Useful for tracking message origin
            and facilitating direct responses. Defaults to None if the message isn't directly associated with an agent.

        to_routing_path (Optional[Union[str, RoutingType]]):
            Optional identifier of the agent intended to receive the message, or a special routing type.
            Used for routing messages to specific agents or groups within the system.
            Special routing types include: '++<all>++', '++<self>++', '++<null>++'.
            Defaults to None if the message is not targeted at a specific agent.

        response_routing_key (Optional[Union[str, RoutingType]]):
            Optional routing key (e.g., queue name) or a special routing type where a response to this message should be sent.
            Defaults to None if no specific response routing is required.

        timestamp (str):
            Timestamp indicating when the message was created, automatically generated using the
            ISO 8601 format. Provides a standardized time reference for all messages.

        message_id (str):
            Unique identifier for the message, automatically generated with a semantic prefix
            and a 12-character lowercase hexadecimal suffix. Ensures message
            uniqueness and facilitates tracking.

        agent_signature (Optional[str]):
            Optional signature of the agent's private key applied to the message_id of this message (for security).
    """

    message_class: Literal["MessageHeader"] = Field(
        default="MessageHeader", description="Message Header"
    )
    from_agent_id: Optional[str] = Field(
        default=None,
        description="(optional) id of agent from which the message originates",
    )
    to_routing_path: Optional[Union[str, RoutingType]] = Field(
        default=None,
        description="(optional) id of agent to which the message is sent. Special routing can be achieved with RoutingType: '++<all>++', '++<self>++', '++<null>++'",
    )
    response_routing_key: Optional[str] = Field(
        default=None,
        description="(optional) Direct request to send responses to this message to a specific routing path. Defaults to None if no specific response routing is required.",
    )
    timestamp: str = Field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat(),
        description="Timestamp of when the message envelope was created.",
    )
    message_id: str = Field(
        default_factory=lambda: new_id("msg_id"),
        description="Unique identifier for the message envelope.",
    )
    agent_signature: Optional[str] = Field(
        default=None,
        description="(optional) signature of agent's private key applied to the message_id of this message (for security)",
    )
    outbox_action_id: Optional[str] = Field(
        default=None,
        description="Optional runtime action outbox record id for idempotent dispatch diagnostics.",
    )
    idempotency_key: Optional[str] = Field(
        default=None,
        description="Optional logical idempotency key for at-least-once delivery handling.",
    )

    model_config = ConfigDict(extra="allow")


class ManagerEscalationContext(BaseModel):
    """Worker-origin metadata for manager-mediated escalation tasks."""

    escalation_id: str = Field(
        default_factory=lambda: new_id("mgr_escalate"),
        description="Stable identifier for this manager escalation.",
    )
    escalation_type: Literal[
        "needs_user_input",
        "needs_specialist_support",
        "worker_result",
        "manager_review",
    ] = Field(description="Why the worker is escalating to TeamArchitect.")
    source_task_id: Optional[str] = Field(
        default=None, description="Worker-local task id that created the escalation."
    )
    source_schedule_id: Optional[str] = Field(
        default=None,
        description="Schedule id when the escalation came from scheduled work.",
    )
    source_agent_id: Optional[str] = Field(default=None, description="Worker agent id.")
    source_agent_name: Optional[str] = Field(
        default=None, description="Worker display/name."
    )
    source_named_agent_type: Optional[str] = Field(
        default=None, description="Worker named agent type."
    )
    original_requester: Literal[
        "worker_schedule", "worker_task", "worker_autonomous"
    ] = Field(
        default="worker_task",
        description="Origin class for work that did not necessarily start as a manager delegation.",
    )
    target_specialist_type: Optional[str] = Field(
        default=None,
        description="Requested specialist type when escalation_type is needs_specialist_support.",
    )
    choice_mode: Optional[Literal["single", "multiple"]] = Field(
        default=None,
        description="Suggested user-choice mode when escalation_type is needs_user_input.",
    )
    choices: List[TaskQueryChoiceOption] = Field(
        default_factory=list,
        description="Stable user-choice options TeamArchitect may present in a TaskQuery.",
    )
    proposed_config_changes: List[Dict[str, Any]] = Field(
        default_factory=list,
        description="Optional non-secret config/runtime changes requiring manager or user approval.",
    )


class MessageMeta(BaseModel):
    """
    Optional metadata accompanying the message content.

    Attributes:
        from_agent_name (Optional[str]): (optional) human-readable name of agent from which the message originates
        to_agent_name (Optional[str]): (optional) human-readable name of agent to which the message is sent. Only used when direct agent-to-agent messaging is required. Only names of existing agents are acceptable. Ignored if to_named_agent_type is defined.
        to_named_agent_type (Optional[str]): (optional) Primary way to route messages (e.g. Tasks / Subtasks) to agents for execution. The named_agent_type corresponds to a queue on which the request will be placed.
        requester_type (Optional[RequesterType]): Type of requester for task requests (user, agent, self, timer)
        is_response_requested (Optional[bool]): Indicates if a response is expected for a task request
        msg_correlation_id (Optional[str]): (optional) request-local correlation id used to correlate requests and responses.
        conversation_id (Optional[str]): (optional) durable identifier for a multi-turn user-visible conversation thread.
        followup_to_task_id (Optional[str]): (optional) top-level task id this new request follows up on within the conversation thread.
        request_timezone (Optional[str]): (optional) IANA timezone name used to interpret relative dates like "today" and "tomorrow" for this request lineage.
    """

    message_class: Literal["MessageMeta"] = Field(
        default="MessageMeta", description="Message Meta Information"
    )
    from_agent_name: Optional[str] = Field(
        default=None,
        description="(optional) human-readable name of agent from which the message originates",
    )
    to_agent_name: Optional[str] = Field(
        default=None,
        description="(optional) human-readable name of agent to which the message is sent. Only used when direct agent-to-agent messaging is required. Only names of existing agents are acceptable. Ignored if to_named_agent_type is defined.",
    )
    to_named_agent_type: Optional[str] = Field(
        default=None,
        description="(optional) Primary way to route messages (e.g. Tasks / Subtasks) to agents for execution. The named_agent_type corresponds to a queue on which the request will be placed.",
    )
    requester_type: Optional[RequesterType] = Field(
        default=None,
        description="Type of requester for task requests (user, agent, self, timer)",
    )
    is_response_requested: Optional[bool] = Field(
        default=False,
        description="Indicates if a response is expected for a task request",
    )
    msg_correlation_id: Optional[str] = Field(
        default=None, description="(optional) request-local correlation identifier"
    )
    conversation_id: Optional[str] = Field(
        default=None,
        description="(optional) durable identifier for the conversation thread this message belongs to",
    )
    followup_to_task_id: Optional[str] = Field(
        default=None,
        description="(optional) top-level task id this request follows up on within the same conversation",
    )
    objective_id: Optional[str] = Field(
        default=None,
        description="(optional) manager-local Objective id for this request lineage",
    )
    objective_work_session_id: Optional[str] = Field(
        default=None,
        description="(optional) bounded Objective work-session id for this request lineage",
    )
    request_timezone: Optional[str] = Field(
        default=None,
        description="(optional) IANA timezone name used to interpret relative dates for this request lineage",
    )
    gateway_context: Optional["GatewayContext"] = Field(
        default=None,
        description="(optional) grouped gateway/session/channel metadata that should survive scheduling and replies",
    )
    manager_escalation: Optional[ManagerEscalationContext] = Field(
        default=None,
        description="(optional) worker-origin metadata for manager-mediated escalation",
    )
    redispatch_notice: Optional[TaskRedispatchNoticeV1] = Field(
        default=None,
        description="(optional) set by the TeamLead when it sends a task out again after a worker handed it back; the receiving worker tells the model what an interrupted run already did",
    )

    model_config = ConfigDict(extra="allow")


# ``GatewayContext.delivery_mode`` of a message a person sent in a chat and
# waits on; other modes (scheduled, heartbeat, autonomous) run unattended.
DELIVERY_MODE_INTERACTIVE = "interactive"


# Identity and personal delivery fields travel together. Runtime signing,
# schedule refresh and persistence loading use the same list when replacing
# or removing a person's context; the contract owns it to avoid runtime cycles.
PERSON_GATEWAY_FIELDS = (
    "task_context",
    "schedule_run",
    "canonical_user_id",
    "user_id",
    "gateway_instance_name",
    "session_key",
    "channel_name",
    "chat_id",
    "thread_id",
)


class GatewayContext(BaseModel):
    """
    Optional gateway-specific metadata that should survive task scheduling and replies.

    This keeps channel/session/delivery information grouped under one stable
    sub-object instead of spreading many transport-specific fields across
    MessageMeta.
    """

    message_class: Literal["GatewayContext"] = Field(
        default="GatewayContext", description="Gateway Context"
    )
    gateway_instance_name: Optional[str] = Field(
        default=None, description="Logical gateway instance name."
    )
    session_key: Optional[str] = Field(
        default=None, description="Gateway session key used to group user interactions."
    )
    channel_name: Optional[str] = Field(
        default=None, description="Logical channel name such as telegram or local."
    )
    user_id: Optional[str] = Field(
        default=None, description="Gateway/user identifier from the channel layer."
    )
    canonical_user_id: Optional[str] = Field(
        default=None,
        description="Stable gateway-owned user identifier used for usermem ownership.",
    )
    chat_id: Optional[str] = Field(
        default=None, description="Channel conversation identifier."
    )
    thread_id: Optional[str] = Field(
        default=None,
        description="Optional channel thread or topic identifier scoped within chat_id.",
    )
    delivery_mode: Optional[str] = Field(
        default=None,
        description="Gateway delivery mode such as interactive, cron, heartbeat, or deferred.",
    )
    scheduled_job_id: Optional[str] = Field(
        default=None,
        description="Stable scheduled job identifier when the task originated from scheduled work.",
    )
    schedule_owner_agent_id: Optional[str] = Field(
        default=None,
        description="Agent id that owns the scheduled message persistence/execution.",
    )
    heartbeat_source_id: Optional[str] = Field(
        default=None,
        description="Canonical heartbeat source identifier for this gateway instance.",
    )
    original_message_id: Optional[str] = Field(
        default=None,
        description="Optional channel message identifier that originated the request.",
    )
    task_context: Optional[SignedTaskContextV1] = Field(
        default=None,
        description=(
            "Signed statement of the person (and roles) this work is for, from the "
            "gateway or scheduler (access_control.identity). Copied unchanged onto "
            "delegated tasks; enforcement points trust only this, not canonical_user_id."
        ),
    )
    schedule_run: Optional[ScheduleRunIdentityV1] = Field(
        default=None,
        description=(
            "Who a scheduled run works for when the agent that started it holds no "
            "scheduler key: the schedule's manager-signed permit and this run's own "
            "fields (access_control.task_identity). Set instead of task_context, never "
            "with it; copied unchanged onto delegated tasks and never saved on a schedule."
        ),
    )
    # The description still names the switch's old label (now Run
    # concurrently): it is part of the frozen fingerprint of the team-transfer
    # schedules section (schedules/6), and a wording change alone is not worth
    # a new section version and conversion step.
    parallel_thread: bool = Field(
        default=False,
        description=(
            "The request was sent in a thread the person started with Web Chat's Launch "
            "parallel thread: the team lead may run it at the same time as its other work "
            "(custom_agents.execution_lanes). Scheduling only; it grants nothing."
        ),
    )


# Resolve the forward GatewayContext annotation within its owning contract.
MessageMeta.model_rebuild()
