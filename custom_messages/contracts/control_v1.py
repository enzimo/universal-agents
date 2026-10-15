"""control v1 contracts, independent of runtime hosts and SDKs."""

from custom_messages.contracts.access_v2 import PersonStanding
from custom_messages.contracts.status_v1 import StatusFindingV1
from custom_messages.enums import ControlType
from custom_messages.enums import ExecutionPlanItemStatus
from custom_messages.enums import MessageMetaType
from custom_messages.enums import ObjectiveHorizon
from custom_messages.enums import ObjectivePriority
from custom_messages.enums import ObjectiveStatus
from custom_messages.enums import ObjectiveWorkPolicyStatus
from custom_messages.enums import ProgressState
from custom_messages.records.objectives import ObjectiveSuccessCriterion
from enum import StrEnum
from id_generation import new_id
from pydantic import BaseModel
from pydantic import Field
from typing import Any
from typing import Dict
from typing import List
from typing import Literal
from typing import Optional


class Instruction(BaseModel):
    """
    Definition of a specific instruction.

    Attributes:
        instruction (str): A specific instruction as a string. If used in the context of a ControlInstruction, it directs the Agent to do something: exit, pause, resume or info instructions: 'basic', 'full'
    """

    instruction_id: str = Field(
        default_factory=lambda: new_id("instruction_id"),
        description="Unique identifier for the instruction.",
    )
    message_class: Literal["Instruction"] = Field(
        default="Instruction", description="Instruction"
    )
    instruction: str = Field(
        description="A specific instruction as a string. If used in the context of a ControlInstruction, it directs the Agent to do something: exit, pause, resume or info instructions: 'basic', 'full'"
    )

    def get_id(self) -> str:
        return self.instruction_id


class NoOpMessage(BaseModel):
    """
    NOOP Message that does not get acted upon but might carry some general information output by the LLM.
    Could be used for LLM to output acknowledgement of receiving some information.

    Attributes:
        info_message (str): An optional informational message carried by the NOOP message.
    """

    noop_id: str = Field(
        default_factory=lambda: new_id("noop_id"),
        description="Unique identifier for the noop.",
    )
    message_class: Literal["NoOpMessage"] = Field(
        default="NoOpMessage", description="No-Op Message"
    )
    info_message: Optional[str] = Field(
        description="An optional informational message carried by the NOOP message."
    )

    def get_id(self) -> str:
        return self.noop_id


class ControlInstruction(BaseModel):
    """
    A message type for controlling the behavior of agents or teams.

    Attributes:
        type (ControlType): The type of control message being sent, such as a control command or a status request.
        instruction (Instruction): The instruction to execute, if any. This could include commands like 'exit', 'pause', or 'resume'.
    """

    instruction_id: str = Field(
        default_factory=lambda: new_id("instruction_id"),
        description="Unique identifier for the instruction.",
    )
    message_class: Literal["ControlInstruction"] = Field(
        default="ControlInstruction", description="Control Instruction"
    )
    type: ControlType
    instruction: Instruction
    target_task_id: Optional[str] = Field(
        default=None,
        description="Optional active task identifier targeted by this control message.",
    )
    target_correlation_id: Optional[str] = Field(
        default=None,
        description="Optional request-local correlation id targeted by this control message.",
    )
    reason: Optional[str] = Field(
        default=None,
        description="Optional operator-visible reason for the control action.",
    )
    cascade_to_subtasks: bool = Field(
        default=True,
        description="Whether the runtime should also stop any outstanding delegated subtasks for the targeted task.",
    )
    runtime_settings: Dict[str, Any] = Field(
        default_factory=dict,
        description="Optional structured runtime settings payload for non-task operational controls.",
    )

    def get_id(self) -> str:
        return self.instruction_id


class AgentCreationRequest(BaseModel):
    """
    Message type for requesting the instantiation of a new AI Agent.
    This message allows for specifying the desired quantity and type of agent to be created.

    Attributes:
        agent_quantity (int):
            The number of agents of this type to create. Defaults to 1.
        named_agent_type (Optional[str]):
            The class of the agent to create (from available agent options).
            If None, a default agent type will be used.
    """

    request_id: str = Field(
        default_factory=lambda: new_id("request_id"),
        description="Unique identifier for the request.",
    )
    message_class: Literal["AgentCreationRequest"] = Field(
        default="AgentCreationRequest", description="Agent Creation Request"
    )
    agent_quantity: int = Field(default=1, ge=1, le=1000, description="Number of agents of this type, bounded again by current team capacity.")
    named_agent_type: str = Field(description="Named Type of agent to instantiate.")
    team_id: Optional[str] = Field(
        default="NO_TEAM",
        description="Id of the team for which the agent should be instantiated.",
    )
    mcp_profile_names: Optional[List[str]] = Field(
        default=None,
        description="Optional list of trusted MCP profile names to attach when this agent instance starts.",
    )
    idle_ttl_seconds: Optional[float] = Field(
        default=None,
        gt=0, le=86400, allow_inf_nan=False,
        description="Optional idle TTL for ephemeral dynamically spawned agents. Startup-supervised agents ignore this.",
    )

    def get_id(self) -> str:
        return self.request_id


class AgentStatus(BaseModel):
    """
    Represents the status of an agent, including assignment details, activity state, and other information.
    This class is also used for requesting AgentStatus, in which case only info_type and status_type are required fields.

    Attributes:
        team_id (Optional[str]): ID of the team this agent belongs to. Defaults to 'NO_TEAM'.
        named_agent_type (Optional[str]): Named type of the agent. Will be used during instantiation requests to look up and set agent's parameters (e.g. role, backstory, tools). Not unique - e.g: CodeWritingAgent
        self_id (Optional[str]): ID of current agent. This is synonymous with the ID of the queue on which this agent is listening.
        is_paused (Optional[bool]): Indicates whether the processing is paused.
        is_executing (Optional[bool]): Indicates whether the agent's execution slot is currently occupied.
        has_execution_capacity (Optional[bool]): Indicates whether the agent can immediately start executing another top-level task.
        is_busy (Optional[bool]): Deprecated alias for is_executing.
        name (Optional[str]): Unique (typically randomly generated) name of the agent to help LLMs remember it.
        tools (Optional[List[str]]): List of tools at this agent's disposal. Defaults to an empty list.
        available_skills (Optional[List[str]]): Semantic names of skills this agent can activate. Defaults to an empty list.
        is_exiting (Optional[bool]): Indicates whether the agent is exiting and this is its last status update. Defaults to False.
        is_team_lead (Optional[bool]): Indicates whether this agent is team leader. Defaults to False.
        top_level_queue_depth (Optional[int]): Runnable queued top-level tasks waiting to execute.
        waiting_top_level_task_count (Optional[int]): Top-level tasks blocked in WAITING.
        deferred_message_count (Optional[int]): Inbound messages deferred while execution capacity was unavailable.
        diagnostic (Optional[dict]): Optional diagnostic information like available memory, queue metrics, number of messages in conversation, uptime, current problem being worked etc. Defaults to None.
        agent_pubkey (Optional[str]): Optional agent's public key for secure communication.
        agent_role (Optional[str]): Agent's self-reported role.
        info_type (ControlType): Specifies the type of status information requested or provided (e.g., basic, full). Defaults to ControlType.STATUS_REQUEST_BASIC.
        status_type (MessageMetaType): Indicates whether this is a request for status or a response to a request with own status.
    """

    message_class: Literal["AgentStatus"] = Field(
        default="AgentStatus", description="Agent Status"
    )
    agent_status_id: str = Field(
        default_factory=lambda: new_id("agent_status_id"),
        description="Unique identifier for the task.",
    )
    team_id: Optional[str] = Field(
        default="NO_TEAM", description="Id of the team this agent belongs to"
    )
    named_agent_type: Optional[str] = Field(
        description="Named type of the agent. Will be used during instantiation requests to look up and set agent's parameters (e.g. role, backstory, tools). Not unique - e.g: CodeWritingAgent"
    )
    self_id: Optional[str] = Field(description="Id of current agent")
    is_paused: Optional[bool] = Field(description="Is the processing paused?")
    is_executing: Optional[bool] = Field(
        default=None,
        description="Whether the agent is actively executing work right now.",
    )
    has_execution_capacity: Optional[bool] = Field(
        default=None,
        description="Whether the agent can immediately start executing another top-level task.",
    )
    is_busy: Optional[bool] = Field(
        default=None, description="Deprecated alias for is_executing."
    )
    name: Optional[str] = Field(
        description="Unique (typically randomly generated) name of the agent to help LLMs rember it"
    )
    tools: Optional[List[str]] = Field(
        default_factory=list, description="Tools at this agent's disposal"
    )
    available_skills: Optional[List[str]] = Field(
        default_factory=list,
        description="Semantic names of skills this agent can activate",
    )
    status_findings: List[StatusFindingV1] = Field(
        default_factory=list,
        description="Problems this agent knows about itself, for administrators' Status page (custom_agents.status_findings).",
    )
    is_exiting: Optional[bool] = Field(
        default=False,
        description="Indicates whether the agent is exiting and this is its last status update",
    )
    accepting_work: bool = Field(
        default=True,
        description="False while a worker winds down after losing its launch lease: it still finishes tasks it holds and stays present, but takes no new work, so delegation must not count it as an available worker.",
    )
    is_team_lead: Optional[bool] = Field(
        default=False, description="Indicates whether this agent is team leader"
    )
    top_level_queue_depth: Optional[int] = Field(
        default=None, description="Runnable queued top-level tasks waiting to execute."
    )
    waiting_top_level_task_count: Optional[int] = Field(
        default=None, description="Top-level tasks currently blocked in WAITING."
    )
    deferred_message_count: Optional[int] = Field(
        default=None, description="Inbound messages currently deferred by the agent."
    )
    current_task_id: Optional[str] = Field(
        default=None, description="Current locally executing task id, when known."
    )
    current_task_age_seconds: Optional[int] = Field(
        default=None, description="Approximate age in seconds of the current task."
    )
    context_window_tokens: Optional[int] = Field(
        default=None,
        description="Configured or inferred context-window token limit used for pressure estimates.",
    )
    estimated_context_tokens: Optional[int] = Field(
        default=None,
        description="Heuristic estimated tokens in current prompt-relevant context.",
    )
    context_fill_ratio: Optional[float] = Field(
        default=None,
        description="estimated_context_tokens divided by context_window_tokens.",
    )
    context_pressure_level: Optional[str] = Field(
        default=None,
        description="Heuristic context pressure level: low, moderate, high, critical, or unknown.",
    )
    tool_call_budget_fill_ratio: Optional[float] = Field(
        default=None,
        description="Observed tool-call count divided by configured per-task tool-call limit.",
    )
    tool_result_budget_fill_ratio: Optional[float] = Field(
        default=None,
        description="Observed tool-result characters divided by configured invocation result-character limit.",
    )
    diagnostic: Optional[dict] = Field(
        default=None,
        description="(optional) diagnostic information like available memory, number of messages in conversation, uptime, current problem being worked etc.",
    )
    agent_pubkey: Optional[str] = Field(
        default=None,
        description="(optional) agent's public key for secure communication",
    )
    agent_role: Optional[str] = Field(description="Agent's self-reported role")
    last_seen_at: Optional[str] = Field(
        default=None,
        description="ISO 8601 UTC timestamp of when this agent's status was last received.",
    )
    info_type: ControlType = Field(
        default=ControlType.STATUS_REQUEST_BASIC
    )  # specify type of info that is requested (STATUS_REQUEST_BASIC, STATUS_REQUEST_FULL)
    status_type: MessageMetaType = Field(
        ...,
        description="Indicates whether this is a request for status or a response to a request with own status.",
    )

    def get_id(self) -> str:
        return self.agent_status_id

    def model_post_init(self, _context: Any) -> None:
        if self.is_executing is None and self.is_busy is not None:
            self.is_executing = self.is_busy
        if self.is_busy is None and self.is_executing is not None:
            self.is_busy = self.is_executing


class AgentStatusMap(BaseModel):
    """Map of received AgentStatus messages"""

    message_class: Literal["AgentStatusMap"] = Field(
        default="AgentStatusMap", description="Agent Status Map"
    )
    agent_map: Dict[str, AgentStatus] = Field(default_factory=dict)


class SystemStatusMessage(BaseModel):
    """Message describing some system status information"""

    status_id: str = Field(
        default_factory=lambda: new_id("status_id"),
        description="Unique identifier for the task.",
    )
    message_class: Literal["SystemStatusMessage"] = Field(
        default="SystemStatusMessage", description="System Status Message"
    )
    system_status: str = Field(
        ...,
        description="Notification or status of the system communicated to the agent.",
    )

    def get_id(self) -> str:
        return self.status_id


class RuntimeWorkChildSummary(BaseModel):
    """Compact operator-facing state for one task in an execution graph."""

    task_id: str
    state: ProgressState
    target_agent_name: Optional[str] = None
    target_named_agent_type: Optional[str] = None
    last_updated_at: Optional[str] = None


class RuntimeWorkSummary(BaseModel):
    """One authenticated user's nonterminal execution root."""

    root_task_id: str
    state: ProgressState
    problem_statement: str = ""
    is_current: bool = False
    queue_position: Optional[int] = None
    session_key: Optional[str] = None
    conversation_id: Optional[str] = None
    objective_id: Optional[str] = None
    objective_work_session_id: Optional[str] = None
    plan_id: Optional[str] = None
    latest_activity_at: Optional[str] = None
    latest_activity: Optional[str] = None
    blocker: Optional[str] = None
    children: List[RuntimeWorkChildSummary] = Field(default_factory=list)


class RuntimeWorkScheduleSummary(BaseModel):
    """Future scheduled intent shown separately from executing work."""

    schedule_id: str
    schedule_name: Optional[str] = None
    objective_id: Optional[str] = None
    enabled: bool = True
    paused_until: Optional[str] = None
    next_fire_at: Optional[str] = None


class RuntimeObjectiveSubgoal(BaseModel):
    """One current Plan step projected into authenticated Objective controls."""

    step_id: str
    title: str
    status: ExecutionPlanItemStatus
    status_detail: Optional[str] = None
    result_summary: Optional[str] = None


class RuntimeObjectiveSummary(BaseModel):
    """Authoritative user-owned Objective state for operator controls."""

    objective_id: str
    record_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    title: str
    desired_outcome: str
    status: ObjectiveStatus
    horizon: ObjectiveHorizon
    priority: ObjectivePriority
    progress_summary: Optional[str] = None
    current_focus: Optional[str] = None
    current_plan_id: Optional[str] = None
    plan_summary: Optional[str] = None
    subgoals: List[RuntimeObjectiveSubgoal] = Field(default_factory=list)
    success_criteria: List[ObjectiveSuccessCriterion] = Field(default_factory=list)
    schedule_ids: List[str] = Field(default_factory=list)
    heartbeat_item_ids: List[str] = Field(default_factory=list)
    work_policy_status: Optional[ObjectiveWorkPolicyStatus] = None
    updated_at: str
    deletion_requested_at: Optional[str] = None
    deleted_at: Optional[str] = None
    deletion_summary: Optional[str] = None
    available_actions: List[Literal["pause", "resume", "stop", "abandon", "delete"]] = (
        Field(default_factory=list)
    )


class RuntimeWorkCancellationReceipt(BaseModel):
    """Manager-confirmed result for one execution-root cancellation."""

    root_task_id: str
    confirmed: bool
    cancelled_task_count: int = 0
    stop_signals_sent: int = 0
    unconfirmed_worker_count: int = 0
    message: str = ""


class FinishedTaskSummary(BaseModel):
    task_id: str
    state: str
    problem_statement: str
    owner_agent_id: Optional[str] = None
    owner_agent_name: Optional[str] = None
    finished_at: Optional[str] = None
    answer: str = ""


class ManagerRuntimeWorkCommand(BaseModel):
    """Authenticated gateway-to-manager inventory or cancellation request."""

    message_class: Literal["ManagerRuntimeWorkCommand"] = "ManagerRuntimeWorkCommand"
    command_id: str = Field(default_factory=lambda: new_id("mgr_work"))
    command_type: MessageMetaType = MessageMetaType.REQUEST
    action: Literal[
        "inventory",
        "cancel",
        "list_tasks",
        "list_finished_tasks",
        "remove_task",
        "purge_tasks",
        "list_objectives",
        "pause_objective",
        "resume_objective",
        "stop_objective",
        "abandon_objective",
        "delete_objective",
    ]
    scope: Literal["task", "conversation", "session", "user", "objective"]
    target_task_id: Optional[str] = None
    target_objective_id: Optional[str] = None
    expected_objective_digest: Optional[str] = Field(
        default=None,
        pattern=r"^[0-9a-f]{64}$",
    )
    session_key: Optional[str] = None
    conversation_id: Optional[str] = None
    inventory_digest: Optional[str] = None
    task_offset: int = Field(default=0, ge=0)
    task_limit: int = Field(default=20, ge=1, le=20)
    task_total: int = Field(default=0, ge=0)
    queued_task_count: int = Field(default=0, ge=0)
    current_task_count: int = Field(default=0, ge=0)
    reason: str = "Operator requested runtime recovery control."
    success: Optional[bool] = None
    message: str = ""
    objective_status: Optional[ObjectiveStatus] = None
    roots: List[RuntimeWorkSummary] = Field(default_factory=list)
    finished_tasks: List[FinishedTaskSummary] = Field(default_factory=list)
    schedules: List[RuntimeWorkScheduleSummary] = Field(default_factory=list)
    objectives: List[RuntimeObjectiveSummary] = Field(default_factory=list)
    cancellation_receipts: List[RuntimeWorkCancellationReceipt] = Field(
        default_factory=list
    )

    def get_id(self) -> str:
        return self.command_id


class PinnedMemoryScopePayload(BaseModel):
    """Exact team or canonical-user scope for a pinned-memory result."""

    team_id: Optional[str] = None
    canonical_user_id: Optional[str] = None


class PinnedMemoryFactPayload(BaseModel):
    """An active pinned-memory fact as manager-control replies show it.

    Built from named fields (``custom_agents.pinned_memory_runtime``:
    ``public_pinned_memory_fact`` then ``pinned_memory_reply``). Where a fact
    came from (source chat, session, conversation, task, creator) and its
    internal UUID never leave storage: a Signal chat id is a phone number.
    Free-form metadata is left out of replies too; agent tools show it.
    """

    bank: Literal["coremem", "usermem"]
    team_id: str = ""
    canonical_user_id: str = ""
    memory_id: int = Field(ge=1)
    fact_text: str
    valid_from_utc: str
    invalid_after_utc: Optional[str] = None
    tags: List[str] = Field(default_factory=list)
    created_at_utc: str
    updated_at_utc: str
    record_digest: str = Field(pattern=r"^[0-9a-f]{64}$")


class PinnedMemoryCommandResultPayload(BaseModel):
    """Typed action result returned by the manager's pinned-memory owner."""

    ok: bool
    bank: Literal["coremem", "usermem"]
    action: Literal["list", "show", "create", "replace", "delete", "undo"]
    scope: PinnedMemoryScopePayload
    memory_id: Optional[int] = Field(default=None, ge=1)
    event_id: Optional[str] = None
    fact_preview: Optional[str] = None
    fact: Optional[PinnedMemoryFactPayload] = None
    facts: List[PinnedMemoryFactPayload] = Field(default_factory=list)
    range: List[int] = Field(default_factory=list)
    sort: Optional[str] = None
    total: Optional[int] = Field(default=None, ge=0)
    returned: Optional[int] = Field(default=None, ge=0)
    deleted: bool = False
    undone_event_id: Optional[str] = None
    undone_event_type: Optional[str] = None
    message: Optional[str] = None


PINNED_MEMORY_SORT_NEWEST = "newest"


class PinnedMemoryCommandPayload(BaseModel):
    """Transport form of a parsed pinned-memory command."""

    bank: Literal["coremem", "usermem"]
    action: Literal["list", "show", "create", "replace", "delete", "undo"]
    memory_id: Optional[int] = None
    event_id: Optional[str] = None
    fact_text: Optional[str] = None
    tags: Optional[List[str]] = None
    metadata: Optional[Dict[str, Any]] = None
    valid_from_utc: Optional[str] = None
    invalid_after_utc: Optional[str] = None
    timezone_name: Optional[str] = None
    sort: str = "memory_id"
    range_start: int = 1
    range_end: int = 50
    source_key: Optional[str] = None
    expected_record_digest: Optional[str] = Field(
        default=None,
        pattern=r"^[0-9a-f]{64}$",
    )


class ManagerMemoryCommand(BaseModel):
    """Durable gateway-to-manager pinned-memory command."""

    message_class: Literal["ManagerMemoryCommand"] = "ManagerMemoryCommand"
    command_id: str = Field(default_factory=lambda: new_id("manager_memory"))
    command_type: MessageMetaType = MessageMetaType.REQUEST
    command: PinnedMemoryCommandPayload
    success: Optional[bool] = None
    rendered_result: str = ""
    result: Optional[PinnedMemoryCommandResultPayload] = None
    error_code: Optional[
        Literal[
            "authorization_denied",
            "conflict",
            "invalid_request",
            "runtime_error",
        ]
    ] = None

    def get_id(self) -> str:
        return self.command_id


class ManagerTopicCommand(BaseModel):
    """Durable gateway-to-manager conversation-topic command."""

    message_class: Literal["ManagerTopicCommand"] = "ManagerTopicCommand"
    command_id: str = Field(default_factory=lambda: new_id("manager_topic"))
    command_type: MessageMetaType = MessageMetaType.REQUEST
    action: Literal["list_tags", "find_threads", "tag_thread"]
    conversation_id: Optional[str] = None
    tag: Optional[str] = None
    tags: List[str] = Field(default_factory=list)
    private: bool = False
    success: Optional[bool] = None
    known_tags: List[Dict[str, Any]] = Field(default_factory=list)
    thread_matches: List[Dict[str, Any]] = Field(default_factory=list)
    applied_tags: List[str] = Field(default_factory=list)
    message: str = ""

    def get_id(self) -> str:
        return self.command_id


class KnowledgeCommandAction(StrEnum):
    """What a person does on the Web Chat Knowledge page (``ManagerKnowledgeCommand``)."""

    LIST = "list"
    READ = "read"
    HISTORY = "history"
    WRITE = "write"
    ADD_NOTE = "add_note"
    ARCHIVE = "archive"
    REVERT = "revert"
    LIST_CONTRIBUTIONS = "list_contributions"
    # How many of the person's proposals wait (the My proposals badge); only ``total`` is set.
    COUNT_CONTRIBUTIONS = "count_contributions"
    READ_CONTRIBUTION = "read_contribution"
    PUBLISH_CONTRIBUTION = "publish_contribution"
    WITHDRAW_CONTRIBUTION = "withdraw_contribution"


# Actions that only read; the rest change the knowledge base.
KNOWLEDGE_READ_ACTIONS = frozenset(
    {
        KnowledgeCommandAction.LIST,
        KnowledgeCommandAction.READ,
        KnowledgeCommandAction.HISTORY,
        KnowledgeCommandAction.LIST_CONTRIBUTIONS,
        KnowledgeCommandAction.COUNT_CONTRIBUTIONS,
        KnowledgeCommandAction.READ_CONTRIBUTION,
    }
)
# Actions on the person's own pending team contributions, not on a page space.
KNOWLEDGE_CONTRIBUTION_ACTIONS = frozenset(
    {
        KnowledgeCommandAction.LIST_CONTRIBUTIONS,
        KnowledgeCommandAction.COUNT_CONTRIBUTIONS,
        KnowledgeCommandAction.READ_CONTRIBUTION,
        KnowledgeCommandAction.PUBLISH_CONTRIBUTION,
        KnowledgeCommandAction.WITHDRAW_CONTRIBUTION,
    }
)
KNOWLEDGE_PAGE_SIZE = 20
KnowledgeSpaceName = Literal["team", "personal"]


class KnowledgeCommandPayload(BaseModel):
    """One Knowledge page request. It names no person: the manager acts for the
    gateway-signed person only (``verified_command_person``), and ``personal``
    always means that person's own pages."""

    action: KnowledgeCommandAction
    space: KnowledgeSpaceName = "team"
    # Lists choose current or archived pages; merged names are never listed.
    status: Literal["active", "archived"] = "active"
    # The page's name within the space (``github-release-process``); empty for lists and new pages.
    page: str = Field(default="", max_length=240)
    query: str = Field(default="", max_length=400)
    offset: int = Field(default=0, ge=0)
    limit: int = Field(default=KNOWLEDGE_PAGE_SIZE, ge=1, le=KNOWLEDGE_PAGE_SIZE)
    # read: an earlier revision to show; revert: the revision to restore.
    revision: int = Field(default=0, ge=0)
    # The revision the person's change is based on (0 creates a page).
    base_revision: int = Field(default=0, ge=0)
    title: str = Field(default="", max_length=1000)
    # Omitted keeps the existing summary; an explicit empty string clears it.
    summary: Optional[str] = Field(default=None, max_length=4000)
    body: str = Field(default="", max_length=500000)
    tags: Optional[List[str]] = Field(default=None, max_length=100)
    note: str = Field(default="", max_length=50000)
    # The person saw the review flags and chose to save anyway.
    confirm_review: bool = False
    contribution_id: str = Field(default="", max_length=240)
    # publish_contribution: publish the person's edited version instead of the proposal as written.
    edited: bool = False


class KnowledgeEditorPayload(BaseModel):
    """Who made a change. ``person_id`` is set by the manager only for a
    person's own edit in Web Chat; before answering the browser the gateway
    writes ``label`` (an agent type, "You" or the person's name) and clears the
    id, so the browser never sees person ids."""

    kind: Literal["agent", "person", "system"]
    agent_type: str = ""
    person_id: str = ""
    label: str = ""


class KnowledgePageSummaryPayload(BaseModel):
    space: KnowledgeSpaceName
    page: str
    title: str
    page_type: str
    summary: str = ""
    tags: List[str] = Field(default_factory=list)
    revision: int = Field(ge=1)
    status: Literal["active", "archived"]
    updated_at_utc: str
    # Search matches with the matched words in [brackets]; empty in plain lists.
    snippet: str = ""


class KnowledgeNotePayload(BaseModel):
    note_id: str
    body: str
    created_at_utc: str
    editor: KnowledgeEditorPayload


class KnowledgeLinkPayload(BaseModel):
    """One ``[[link]]`` in the shown text: as written, the page it opens, and whether that page exists."""

    written: str
    space: KnowledgeSpaceName
    page: str
    exists: bool


class KnowledgePageDetailPayload(KnowledgePageSummaryPayload):
    body: str
    created_at_utc: str
    # Agents may not edit the page before this time (people may); empty when not frozen.
    frozen_until_utc: str = ""
    # The revision shown when it is not the current one (``read`` with ``revision``).
    shown_revision: int = Field(default=0, ge=0)
    last_editor: Optional[KnowledgeEditorPayload] = None
    backlinks: List[KnowledgePageSummaryPayload] = Field(default_factory=list)
    open_notes: List[KnowledgeNotePayload] = Field(default_factory=list)
    # The person's own pending proposals for this page.
    pending_contributions: int = Field(default=0, ge=0)
    # Where the ``[[links]]`` in the shown text lead.
    links: List[KnowledgeLinkPayload] = Field(default_factory=list)
    # The merged page's name the request used when it led here (``team/old-name``).
    redirected_from: str = ""


class KnowledgeRevisionPayload(BaseModel):
    revision: int = Field(ge=1)
    title: str
    status: Literal["active", "archived"]
    change_kind: str
    change_summary: str = ""
    created_at_utc: str
    editor: KnowledgeEditorPayload


class KnowledgeReviewFlagPayload(BaseModel):
    """Something on a team-bound change that needs a person's decision, or that
    is never stored. Kind, label and place only, never the matched text."""

    kind: str
    label: str
    # Which field it is in (title, summary, body, tags, note); empty when not tied to one.
    field: str = ""
    line: int = Field(default=0, ge=0)
    column: int = Field(default=0, ge=0)


class KnowledgeContributionPayload(BaseModel):
    """One of the person's own proposed team changes, with the page as it is now."""

    contribution_id: str
    kind: Literal["create", "edit", "note", "archive"]
    page: str
    title: str
    page_type: str
    summary: str = ""
    tags: List[str] = Field(default_factory=list)
    body: str = ""
    change_summary: str = ""
    base_revision: int = Field(default=0, ge=0)
    agent_type: str = ""
    created_at_utc: str
    review_flags: List[KnowledgeReviewFlagPayload] = Field(default_factory=list)
    # The team page now, for before/after; absent for a proposed new page that does not exist.
    current: Optional[KnowledgePageDetailPayload] = None


class KnowledgeCommandResultPayload(BaseModel):
    action: KnowledgeCommandAction
    pages: List[KnowledgePageSummaryPayload] = Field(default_factory=list)
    total: int = Field(default=0, ge=0)
    page: Optional[KnowledgePageDetailPayload] = None
    revisions: List[KnowledgeRevisionPayload] = Field(default_factory=list)
    contributions: List[KnowledgeContributionPayload] = Field(default_factory=list)
    contribution: Optional[KnowledgeContributionPayload] = None
    # Flags the person confirmed when saving.
    review_flags: List[KnowledgeReviewFlagPayload] = Field(default_factory=list)


class ManagerKnowledgeCommand(BaseModel):
    """Durable gateway-to-manager command from a person's Web Chat Knowledge page.

    The manager runs it with ``knowledge_base.service.KnowledgeService`` as a
    person (``ActorKind.PERSON``) built from the gateway-signed context only
    (``custom_agents.knowledge_command_runtime``).
    """

    message_class: Literal["ManagerKnowledgeCommand"] = "ManagerKnowledgeCommand"
    command_id: str = Field(default_factory=lambda: new_id("mgr_knowledge"))
    command_type: MessageMetaType = MessageMetaType.REQUEST
    command: KnowledgeCommandPayload
    success: Optional[bool] = None
    result: Optional[KnowledgeCommandResultPayload] = None
    error_code: Optional[
        Literal[
            "authorization_denied",
            "conflict",
            "page_exists",
            "merged",
            "review_required",
            "sensitive_content",
            "not_found",
            "invalid_request",
            "knowledge_off",
            "runtime_error",
        ]
    ] = None
    # Plain words for the person; never the content a rule refused.
    error_message: str = ""
    review_flags: List[KnowledgeReviewFlagPayload] = Field(default_factory=list)
    current_revision: int = Field(default=0, ge=0)
    # ``merged``: the page a merged page's name now leads to (``team/new-name``).
    merged_into: str = ""

    def get_id(self) -> str:
        return self.command_id


class MovedTeamScheduleV1(BaseModel):
    """A team schedule that moved from a departed person to another administrator."""

    schedule_id: str
    schedule_name: str = ""
    new_owner_person_id: str


class PersonWorkOutcomeV1(BaseModel):
    """What the manager did with one person's work and records (``PersonStandingCommandV1``).

    Counts are what this run changed; a repeat after everything was done
    reports zeros. ``problems`` names what could not be done, in plain words.
    """

    standing: Optional[PersonStanding] = None
    tasks_stopped: int = Field(default=0, ge=0)
    task_records_deleted: int = Field(default=0, ge=0)
    objectives_stopped: int = Field(default=0, ge=0)
    objectives_deleted: int = Field(default=0, ge=0)
    schedules_deleted: int = Field(default=0, ge=0)
    team_schedules_moved: List[MovedTeamScheduleV1] = Field(default_factory=list)
    memory_review_jobs_deleted: int = Field(default=0, ge=0)
    held_record_count: int = Field(default=0, ge=0)
    restored_record_count: int = Field(default=0, ge=0)
    sandbox_deleted: bool = False
    problems: List[str] = Field(default_factory=list)


class PersonStandingCommandV1(BaseModel):
    """Gateway-to-manager: a person's standing changed; bring the manager's records in line.

    Sent by the gateway after ``/user disable``, ``/user remove``,
    ``/user enable``, adding someone back and ``/user restore``. The manager
    acts on the person's standing in the gateway's people copy
    (``access_control.people_directory``), not on ``standing`` here, so a late
    or repeated command never acts on an old standing; ``standing`` is what
    the gateway expects, and a mismatch is logged. Only the gateway may send
    it: the manager requires the gateway-signed task context of an active
    administrator (``custom_agents.person_departure_runtime``), and agents
    cannot send manager control messages at all. Like every gateway command,
    it names the acting person only through that signed context
    (``custom_agents.manager_control_runtime.verified_command_person``).
    """

    message_class: Literal["PersonStandingCommandV1"] = "PersonStandingCommandV1"
    command_id: str = Field(default_factory=lambda: new_id("person_standing"))
    command_type: MessageMetaType = MessageMetaType.REQUEST
    person_id: str = Field(min_length=1, max_length=240)
    standing: PersonStanding
    success: Optional[bool] = None
    message: str = ""
    outcome: Optional[PersonWorkOutcomeV1] = None

    def get_id(self) -> str:
        return self.command_id
