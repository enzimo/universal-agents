"""envelope v1 contracts, independent of runtime hosts and SDKs."""

from custom_messages.contracts.access_v2 import AccessRequestResolvedV1
from custom_messages.contracts.control_v1 import AgentCreationRequest
from custom_messages.contracts.control_v1 import AgentStatus
from custom_messages.contracts.control_v1 import ControlInstruction
from custom_messages.contracts.control_v1 import Instruction
from custom_messages.contracts.control_v1 import ManagerKnowledgeCommand
from custom_messages.contracts.control_v1 import ManagerMemoryCommand
from custom_messages.contracts.control_v1 import ManagerRuntimeWorkCommand
from custom_messages.contracts.control_v1 import ManagerTopicCommand
from custom_messages.contracts.control_v1 import NoOpMessage
from custom_messages.contracts.control_v1 import PersonStandingCommandV1
from custom_messages.contracts.control_v1 import SystemStatusMessage
from custom_messages.contracts.delegated_work_v1 import DelegatedTaskClaimedV1
from custom_messages.contracts.credentials_v1 import ToolSetupRequestV1
from custom_messages.contracts.credentials_v1 import ToolSetupResultV1
from custom_messages.contracts.credentials_v1 import SecureCredentialCollectionRequest
from custom_messages.contracts.credentials_v1 import SecureCredentialCollectionResult
from custom_messages.contracts.credentials_v1 import SignupAutomationRequest
from custom_messages.contracts.credentials_v1 import SignupAutomationResult
from custom_messages.contracts.delivery_v1 import DeliveryReceipt
from custom_messages.contracts.delivery_v1 import UserNotification
from custom_messages.contracts.routing_v1 import MessageHeader
from custom_messages.contracts.routing_v1 import MessageMeta
from custom_messages.contracts.schedule_v1 import AutomationMessage
from custom_messages.contracts.schedule_v1 import ManagerScheduleCommand
from custom_messages.contracts.schedule_v1 import ScheduledMessage
from custom_messages.contracts.scheduled_run_context_v1 import ScheduledRunContextReplyV1
from custom_messages.contracts.scheduled_run_context_v1 import ScheduledRunContextRequestV1
from custom_messages.contracts.task_v1 import AfterActionReport
from custom_messages.contracts.task_v1 import Task
from custom_messages.contracts.task_v1 import TaskProgressStatus
from custom_messages.contracts.task_v1 import TaskQuery
from custom_messages.contracts.task_v1 import TaskStatus
from custom_messages.contracts.task_v1 import TaskSteering
from custom_messages.contracts.worker_handback_v1 import WorkerTaskHandbackV1
from custom_messages.skill_catalog import ManagedSkillCatalogChangedV1
from pydantic import BaseModel
from pydantic import Field
from typing import Annotated
from typing import Literal
from typing import Optional
from typing import Union


ContentType = Annotated[
    Union[
        AccessRequestResolvedV1,
        AgentCreationRequest,
        AgentStatus,
        AfterActionReport,
        AutomationMessage,
        ControlInstruction,
        DelegatedTaskClaimedV1,
        DeliveryReceipt,
        Instruction,  # Used directly? Or always via ControlPayload? Let's assume via ControlPayload
        ManagerKnowledgeCommand,
        ManagerMemoryCommand,
        ManagerRuntimeWorkCommand,
        ManagerScheduleCommand,
        ManagerTopicCommand,
        ManagedSkillCatalogChangedV1,
        NoOpMessage,
        PersonStandingCommandV1,
        ScheduledMessage,
        ScheduledRunContextReplyV1,
        ScheduledRunContextRequestV1,
        ToolSetupRequestV1,
        ToolSetupResultV1,
        SecureCredentialCollectionRequest,
        SecureCredentialCollectionResult,
        SignupAutomationRequest,
        SignupAutomationResult,
        SystemStatusMessage,
        Task,
        TaskQuery,
        TaskProgressStatus,
        TaskSteering,
        TaskStatus,
        UserNotification,
        WorkerTaskHandbackV1,
        # TeamCreationRequest,
        # UserProvidedResponse,
        # UserInputRequest,
        # Add any other distinct content payload classes here
        # If a message has *no* specific payload beyond header/meta, use a placeholder:
        # type(None) # or a specific EmptyContent(BaseModel) class
    ],
    Field(discriminator="message_class"),
]

# Requests only the gateway sends to the manager's control route. Transport
# routing, the gateway's reply matching, the manager's admission rules and the
# agent-forgery guard (custom_tools.providers.communication) all use this one list.
GATEWAY_MANAGER_CONTROL_TYPES = (
    ManagerKnowledgeCommand,
    ManagerMemoryCommand,
    ManagerRuntimeWorkCommand,
    ManagerScheduleCommand,
    ManagerTopicCommand,
    PersonStandingCommandV1,
)

# Messages only agent runtimes exchange to get a scheduled run its signed task
# context (custom_agents.scheduled_run_contexts). They travel on their own
# Core NATS subjects, never through an agent's work queues, and the
# agent-forgery guard (custom_tools.providers.communication) refuses them
# from agents.
SCHEDULED_RUN_CONTEXT_TYPES = (
    ScheduledRunContextRequestV1,
    ScheduledRunContextReplyV1,
)


class CommsMessage(BaseModel):
    """
    The message container using composition to hold content, and optional metadata.
    Serves as the wrapper that will be submitted to an LLM. The LLM is typically expected to produce
    a CommsMessage as its final generated response.
    """

    message_class: Literal["CommsMessage"] = Field(
        default="CommsMessage", description="Comms Message"
    )
    msg_content: ContentType = Field(
        description="The specific payload/content of the message."
    )
    msg_meta: Optional[MessageMeta] = Field(
        default=None, description="Optional additional metadata."
    )


class MessageEnvelope(CommsMessage):
    """
    The message envelope adding header content (for routing and routing metadata)
    """

    message_class: Literal["MessageEnvelope"] = Field(
        default="MessageEnvelope", description="Message Envelope"
    )
    msg_header: MessageHeader = Field(
        default_factory=MessageHeader,
        description="The standard routing and identification header for the message.",
    )
