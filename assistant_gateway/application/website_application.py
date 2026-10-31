"""Application capabilities required specifically by the Website adapter."""

from __future__ import annotations

from assistant_gateway.application.file_retention_page import FileRetentionPage
from custom_messages.contracts.access_v2 import ChangeApprovalRecordV1
from custom_messages.contracts.control_v1 import KnowledgeCommandPayload, KnowledgeCommandResultPayload
from custom_messages.contracts.status_v1 import StatusReportV1

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
import hashlib
import json
from typing import Any, Literal, Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field

from assistant_gateway.application.transient_presentation import PendingCredentialAction
from assistant_gateway.application.access_inbox_ports import AccessInboxApplication
from assistant_gateway.application.account_overview import AccountsPage
from assistant_gateway.application.access_workflows import AccessWorkflowApplication
from assistant_gateway.application.gateway_application import (
    GatewayApplication,
    GatewayApplicationResponse,
    GatewayDeliveryEvent,
    GatewaySession,
)
from assistant_gateway.application.procedures import ProcedureApplication
from assistant_gateway.application.web_chat_threads import WebChatThread
from assistant_gateway.application.route_context import GatewayRouteContextV1
from runtime_settings.appearance import ACCENT_SHARE_RANGE, DARK_INTEGER_RANGES


WEB_CHAT_CHAT_ID = "web-chat"


class WebsiteContractV1(BaseModel):
    """Strict immutable base for Website-only application contracts."""

    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)
    schema_version: Literal[1] = 1


class _WebAppearanceValue(BaseModel):
    """Strict value object nested inside the public appearance contract."""

    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)


def _dark_range(name: str) -> Any:
    """A pydantic field bounded by the shared dark-background range for ``name``."""

    low, high = DARK_INTEGER_RANGES[name]
    return Field(ge=low, le=high)


class WebMetallicAccentV1(_WebAppearanceValue):
    """One metal tint and the share of faces, in percent, that use it."""

    color: str = Field(pattern=r"^#[0-9a-fA-F]{6}$")
    share: int = Field(ge=ACCENT_SHARE_RANGE[0], le=ACCENT_SHARE_RANGE[1])


class WebMetallicAccentPaletteV1(_WebAppearanceValue):
    """The fixed accent set supported by the dark background renderer."""

    gold: WebMetallicAccentV1
    bronze: WebMetallicAccentV1


class WebDarkMetalAppearanceV1(_WebAppearanceValue):
    """Typed dark metallic background settings; ranges come from runtime_settings."""

    detail: Literal["full", "basic"]
    surface_color: str = Field(pattern=r"^#[0-9a-fA-F]{6}$")
    metal_color: str = Field(pattern=r"^#[0-9a-fA-F]{6}$")
    facet_size: int = _dark_range("facet_size")
    facet_depth: int = _dark_range("facet_depth")
    chrome_faces: int = _dark_range("chrome_faces")
    shine: int = _dark_range("shine")
    edge_highlights: int = _dark_range("edge_highlights")
    surface_grain: int = _dark_range("surface_grain")
    light_x: int = _dark_range("light_x")
    light_y: int = _dark_range("light_y")
    light_softness: int = _dark_range("light_softness")
    light_motion: bool
    light_wander: bool
    light_wander_distance: int = _dark_range("light_wander_distance")
    light_speed: int = _dark_range("light_speed")
    mesh_motion: bool
    mesh_speed: int = _dark_range("mesh_speed")
    seed: int = _dark_range("seed")
    accents: WebMetallicAccentPaletteV1


class WebAppearanceV1(WebsiteContractV1):
    """One canonical user's complete, validated Website appearance override."""

    color_mode: Literal["light", "dark"]
    light_colors: tuple[str, str, str, str]
    light_diffusion: int = Field(ge=0, le=100)
    polygon_opacity_low: int = Field(ge=0, le=100)
    polygon_opacity_high: int = Field(ge=0, le=100)
    polygon_line_color: str = Field(pattern=r"^#[0-9a-fA-F]{6}$")
    polygon_line_opacity: int = Field(ge=0, le=100)
    polygon_seed: int = Field(ge=1, le=4_294_967_295)
    dynamic_background: bool
    # A person who never chose keeps interface animation on.
    interface_animation: bool = True
    dark: WebDarkMetalAppearanceV1


class WebMemoryConflictError(ValueError):
    """A digest-bound mutation targeted a stale memory revision."""


class WebTeamMemberStatusV1(WebsiteContractV1):
    """Redacted availability state for one team member."""

    key: Literal["available", "busy", "paused", "stale", "exiting"]
    label: str = Field(min_length=1, max_length=80)
    tone: Literal["success", "warning", "neutral", "danger"]


class WebTeamMemberV1(WebsiteContractV1):
    """Secret-free team member projection rendered by Website Settings."""

    agent_id: str = Field(min_length=1, max_length=240)
    name: str = Field(min_length=1, max_length=240)
    named_agent_type: str = Field(min_length=1, max_length=240)
    is_team_lead: bool
    status: WebTeamMemberStatusV1
    available_skills: tuple[str, ...] = ()
    last_seen_at: datetime


class WebAgentClassV1(WebsiteContractV1):
    named_agent_type: str
    display_name: str
    description: str


class WebTeamMemberListV1(WebsiteContractV1):
    """Versioned team presence result."""

    members: tuple[WebTeamMemberV1, ...]
    definitions: tuple[WebAgentClassV1, ...] = ()


class WebWorkspaceEntryV1(WebsiteContractV1):
    """One safe, secret-free workspace directory entry."""

    path: str = Field(max_length=4096)
    name: str = Field(min_length=1, max_length=1024)
    kind: Literal["directory", "file"]
    size_bytes: int | None = Field(default=None, ge=0)
    modified_at_utc: datetime
    preview_kind: (
        Literal[
            "markdown",
            "html",
            "text",
            "image",
            "pdf",
            "unsupported",
            "too_large",
        ]
        | None
    ) = None
    content_type: str | None = Field(default=None, max_length=240)


class WebWorkspaceDirectoryV1(WebsiteContractV1):
    """One lazily loaded workspace directory page."""

    path: str = Field(max_length=4096)
    entries: tuple[WebWorkspaceEntryV1, ...]
    truncated: bool
    entry_limit: int = Field(ge=1)


class WebWorkspaceFileV1(WebsiteContractV1):
    """Display-safe metadata and optional text for one workspace file."""

    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        str_strip_whitespace=False,
    )

    path: str = Field(min_length=1, max_length=4096)
    name: str = Field(min_length=1, max_length=1024)
    size_bytes: int = Field(ge=0)
    modified_at_utc: datetime
    preview_kind: Literal[
        "markdown",
        "html",
        "text",
        "image",
        "pdf",
        "unsupported",
        "too_large",
    ]
    content_type: str | None = Field(default=None, max_length=240)
    content: str | None = None
    syntax_language: str | None = Field(default=None, max_length=80)
    highlighted_html: str | None = None
    formatted_content: str | None = None
    message: str | None = Field(default=None, max_length=500)


@dataclass(frozen=True)
class GatewayWorkspaceFileContent:
    """Validated raw content that the Website adapter may render inline."""

    path: str
    preview_kind: Literal["html", "image", "pdf"]
    content_type: str
    content: bytes


class WebMemoryV1(WebsiteContractV1):
    """One display-safe active pinned-memory record."""

    scope: Literal["personal", "team"]
    memory_id: int = Field(ge=1)
    fact_text: str = Field(min_length=1)
    tags: tuple[str, ...] = ()
    valid_from_utc: datetime
    invalid_after_utc: datetime | None = None
    created_at_utc: datetime
    updated_at_utc: datetime
    record_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    available_actions: tuple[Literal["edit", "delete"], ...] = ()


class WebMemoryListRequestV1(WebsiteContractV1):
    """Authenticated paginated request for one Settings memory category."""

    context: GatewayRouteContextV1
    scope: Literal["personal", "team"]
    offset: int = Field(default=0, ge=0)
    limit: int = Field(default=20, ge=1, le=100)


class WebMemoryListV1(WebsiteContractV1):
    """Paginated Settings memory category."""

    scope: Literal["personal", "team"]
    memories: tuple[WebMemoryV1, ...]
    offset: int = Field(ge=0)
    limit: int = Field(ge=1, le=100)
    total: int = Field(ge=0)
    has_more: bool
    can_create: bool = False


class WebMemoryCreateRequestV1(WebsiteContractV1):
    """Create a fact in the authenticated user's selected memory bank."""

    context: GatewayRouteContextV1
    scope: Literal["personal", "team"]
    fact_text: str = Field(min_length=1, max_length=12000)
    tags: tuple[str, ...] = ()
    invalid_after_utc: datetime | None = None


class WebMemoryMutationRequestV1(WebsiteContractV1):
    """Digest-bound edit or delete for one Settings memory record."""

    context: GatewayRouteContextV1
    scope: Literal["personal", "team"]
    memory_id: int = Field(ge=1)
    action: Literal["edit", "delete"]
    expected_record_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    fact_text: str | None = Field(default=None, min_length=1, max_length=12000)
    tags: tuple[str, ...] | None = None
    invalid_after_utc: datetime | None = None


class WebMemoryMutationResultV1(WebsiteContractV1):
    """Current memory state or a recoverable delete receipt."""

    scope: Literal["personal", "team"]
    memory_id: int = Field(ge=1)
    action: Literal["create", "edit", "delete"]
    memory: WebMemoryV1 | None = None
    event_id: str | None = None
    deleted: bool = False


class WebMCPConnectionV1(WebsiteContractV1):
    """Secret-free installed MCP connection projection."""

    connection_id: str = Field(min_length=1, max_length=240)
    record_digest: str = Field(min_length=64, max_length=64)
    lifecycle: str = Field(min_length=1, max_length=80)
    connection_name: str = Field(min_length=1, max_length=240)
    transport: str = Field(min_length=1, max_length=80)
    source: str = Field(min_length=1, max_length=80)
    source_locator: str = Field(min_length=1, max_length=2000)
    endpoint: str | None = Field(default=None, max_length=4000)
    server_url_env: str | None = Field(default=None, max_length=240)
    tool_name_prefix: str = Field(default="", max_length=240)
    target_agent_types: tuple[str, ...] = ()
    # The catalog tool (mcp:<name>) whose grants decide who may use the server.
    tool_id: str = Field(min_length=1, max_length=240)
    operation_count: int = Field(ge=0)
    operations: tuple[str, ...] = ()
    credential_mode: Literal["none", "operator_configured", "linked_account"]
    legacy_sse: bool
    # Listed in the operator's configuration when the MCP registry was created.
    from_operator_configuration: bool
    approval: ChangeApprovalRecordV1 | None = None  # how an active connection was approved
    created_at_utc: datetime
    updated_at_utc: datetime
    rejection_reason: str | None = Field(default=None, max_length=4000)
    drift_reason: str | None = Field(default=None, max_length=4000)
    can_decide: bool
    available_actions: tuple[Literal["disable", "remove"], ...] = ()


class WebMCPConnectionListRequestV1(WebsiteContractV1):
    """Authenticated context for listing MCP connections."""

    context: GatewayRouteContextV1


class WebMCPConnectionListV1(WebsiteContractV1):
    """Versioned secret-free MCP connection result."""

    connections: tuple[WebMCPConnectionV1, ...]


class WebHostedMCPServerV1(WebsiteContractV1):
    """Secret-free hosted MCP server projection."""

    hosted_server_id: str = Field(min_length=1, max_length=240)
    definition_digest: str = Field(min_length=64, max_length=64)
    server_name: str = Field(min_length=1, max_length=240)
    purpose: str = Field(min_length=1, max_length=2000)
    lifecycle: str = Field(min_length=1, max_length=80)
    target_agent_types: tuple[str, ...] = ()
    tools: tuple[str, ...] = ()
    bind_host: str = Field(min_length=1, max_length=240)
    port: int = Field(ge=1, le=65535)
    available_actions: tuple[Literal["install", "start", "stop", "remove"], ...] = ()


class WebHostedMCPServerListV1(WebsiteContractV1):
    """Versioned secret-free hosted MCP server result."""

    servers: tuple[WebHostedMCPServerV1, ...]


class WebHostedMCPServerListRequestV1(WebsiteContractV1):
    """Authenticated context for hosted MCP lifecycle visibility."""

    context: GatewayRouteContextV1


class WebObjectiveSubgoalV1(WebsiteContractV1):
    """One typed current-plan step rendered in Objective controls."""

    step_id: str
    title: str
    status: str
    status_detail: str | None = None
    result_summary: str | None = None


class WebObjectiveSuccessCriterionV1(WebsiteContractV1):
    """One typed measurable condition rendered in Objective controls."""

    criterion_id: str
    message_class: Literal["ObjectiveSuccessCriterion"] = "ObjectiveSuccessCriterion"
    description: str
    status: str
    evidence_summary: str | None = None
    updated_at: datetime


class WebObjectiveV1(WebsiteContractV1):
    """Authoritative Objective state available to its canonical owner."""

    objective_id: str
    record_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    title: str
    desired_outcome: str
    status: str
    horizon: str
    priority: str
    progress_summary: str | None = None
    current_focus: str | None = None
    current_plan_id: str | None = None
    plan_summary: str | None = None
    subgoals: tuple[WebObjectiveSubgoalV1, ...] = ()
    success_criteria: tuple[WebObjectiveSuccessCriterionV1, ...] = ()
    schedule_ids: tuple[str, ...] = ()
    heartbeat_item_ids: tuple[str, ...] = ()
    work_policy_status: str | None = None
    updated_at: datetime
    deletion_requested_at: datetime | None = None
    deleted_at: datetime | None = None
    deletion_summary: str | None = None
    available_actions: tuple[
        Literal["pause", "resume", "stop", "abandon", "delete"], ...
    ] = ()


class WebObjectiveListV1(WebsiteContractV1):
    objectives: tuple[WebObjectiveV1, ...]


class WebObjectiveControlRequestV1(WebsiteContractV1):
    context: GatewayRouteContextV1
    objective_id: str = Field(min_length=1, max_length=240)
    action: Literal["pause", "resume", "stop", "abandon", "delete"]
    expected_record_digest: str = Field(pattern=r"^[0-9a-f]{64}$")


class WebRuntimeWorkCancellationReceiptV1(WebsiteContractV1):
    """Typed manager confirmation for one cancelled execution root."""

    root_task_id: str
    confirmed: bool
    cancelled_task_count: int = Field(default=0, ge=0)
    stop_signals_sent: int = Field(default=0, ge=0)
    unconfirmed_worker_count: int = Field(default=0, ge=0)
    message: str = ""


class WebTaskV1(WebsiteContractV1):
    root_task_id: str
    state: str
    problem_statement: str
    is_current: bool
    is_parallel: bool = False
    queue_position: int | None = None
    latest_activity_at: str | None = None
    latest_activity: str | None = None
    blocker: str | None = None
    owner_agent_id: str | None = None
    owner_agent_name: str | None = None
    answer: str | None = None


class WebTaskListRequestV1(WebsiteContractV1):
    context: GatewayRouteContextV1
    agent_id: str = Field(min_length=1, max_length=240)
    offset: int = Field(default=0, ge=0)
    limit: int = Field(default=20, ge=1, le=20)
    view: Literal["active", "finished"] = "active"


class WebTaskListV1(WebsiteContractV1):
    tasks: tuple[WebTaskV1, ...]
    total: int
    queued_count: int
    current_count: int
    inventory_digest: str = Field(pattern=r"^[0-9a-f]{64}$")


class WebTaskControlRequestV1(WebsiteContractV1):
    context: GatewayRouteContextV1
    agent_id: str = Field(min_length=1, max_length=240)
    action: Literal["remove", "purge"]
    task_id: str | None = Field(default=None, min_length=1, max_length=240)
    inventory_digest: str = Field(pattern=r"^[0-9a-f]{64}$")


class WebTaskControlResultV1(WebsiteContractV1):
    success: bool
    message: str
    cancellation_receipts: tuple[WebRuntimeWorkCancellationReceiptV1, ...] = ()


class WebObjectiveControlResultV1(WebsiteContractV1):
    objective_id: str
    action: str
    status: str
    success: bool
    message: str
    cancellation_receipts: tuple[WebRuntimeWorkCancellationReceiptV1, ...] = ()


class WebScheduleWorkerAssignmentV1(WebsiteContractV1):
    """Saved scheduling intent; current eligibility is checked at each launch."""

    agent_type: str | None
    agent_id: str | None
    automatic_creation: Literal["not_configured", "enabled", "disabled", "default_policy"]
    review_agent_type: str | None = None
    upgrade_required: bool = False
    desired_agent_type: str | None = None
    reassignment_pending: bool = False
    # The schedule is stuck: since its missed due time, with what to do.
    not_running_since: datetime | None = None
    not_running_explanation: str | None = None


class WebScheduleV1(WebsiteContractV1):
    schedule_id: str
    record_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    schedule_name: str | None = None
    objective_id: str | None = None
    management_class: str
    system_schedule_kind: str | None = None
    enabled: bool
    paused_until: datetime | None = None
    next_fire_at: datetime | None = None
    worker_assignment: WebScheduleWorkerAssignmentV1
    available_actions: tuple[Literal["pause", "resume", "disable", "remove"], ...] = ()


class WebScheduleListV1(WebsiteContractV1):
    schedules: tuple[WebScheduleV1, ...]


class WebScheduleControlRequestV1(WebsiteContractV1):
    context: GatewayRouteContextV1
    schedule_id: str = Field(min_length=1, max_length=240)
    action: Literal["pause", "resume", "disable", "remove"]
    expected_record_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    paused_until: datetime | None = None


class WebScheduleControlResultV1(WebsiteContractV1):
    schedule_id: str
    action: str
    success: bool


class WebTranscriptAttachmentV1(WebsiteContractV1):
    """Display-safe attachment reference in a durable transcript message."""

    attachment_id: str = Field(min_length=1, max_length=240)
    filename: str = Field(min_length=1, max_length=1024)
    content_type: str = Field(min_length=1, max_length=240)
    kind: str = Field(min_length=1, max_length=80)
    size_bytes: int | None = Field(default=None, ge=0)
    caption: str | None = Field(default=None, max_length=4000)
    url: str = Field(min_length=1, max_length=2000)


class WebTranscriptMessageV1(WebsiteContractV1):
    """Server-authoritative Web Chat presentation message."""

    message_id: str = Field(min_length=1, max_length=240)
    client_request_id: str | None = Field(default=None, min_length=1, max_length=240)
    # Set on an access review announcement: the request the chat card shows.
    access_request_id: str | None = Field(default=None, min_length=1, max_length=240)
    session_key: str = Field(min_length=1, max_length=240)
    conversation_id: str | None = Field(default=None, max_length=240)
    session_sequence: int = Field(ge=1)
    event_sequence: int = Field(ge=1)
    role: Literal["user", "assistant", "system"]
    content: str = Field(max_length=1_000_000)
    message_type: str = Field(min_length=1, max_length=120)
    message_level: str = Field(min_length=1, max_length=80)
    correlation_id: str | None = Field(default=None, max_length=240)
    attachments: tuple[WebTranscriptAttachmentV1, ...] = ()
    delivery_status: str = Field(min_length=1, max_length=80)
    created_at_utc: datetime


class WebTranscriptHistoryPageV1(WebsiteContractV1):
    """One owner-authorized page of durable session history."""

    messages: tuple[WebTranscriptMessageV1, ...]
    before_cursor: str | None = Field(default=None, max_length=4000)
    has_more: bool


class WebTranscriptEventPageV1(WebsiteContractV1):
    """Owner-wide transcript changes after one durable cursor."""

    messages: tuple[WebTranscriptMessageV1, ...]
    next_cursor: str = Field(min_length=1, max_length=4000)
    has_more: bool


class WebChatThreadStatesV1(WebsiteContractV1):
    """Live thread-list state each open page compares on every event read.

    The durable event stream carries only transcript rows, so a rename, an
    archive, a thread started on another device, or a task still running
    would otherwise reach other open pages only on reload.
    """

    # Changes whenever a listed thread appears, disappears, or changes name
    # (a rename, or its first ordinary message naming it); later messages do
    # not change it, so the page reloads the thread list only when it shows
    # something new.
    threads_revision: str = Field(min_length=1, max_length=64)
    # Listed threads where the gateway still waits for the agent's reply.
    busy_session_keys: tuple[str, ...] = ()
    # Listed threads whose current conversation has an open request, from the
    # durable request record that turns a new message into guidance. The
    # Run concurrently switch stays locked for them; busy_session_keys can end
    # sooner (a reply wait times out or the gateway restarts).
    open_request_session_keys: tuple[str, ...] = ()


def web_chat_threads_revision(threads: list[WebChatThread]) -> str:
    """Digest of what the thread list shows, so pages refetch only on change."""

    # The Run concurrently setting is shown too (the switch below Send).
    shown = sorted(
        (thread.session_key, thread.session_alias or "", thread.title or "", thread.parallel_thread)
        for thread in threads
    )
    encoded = json.dumps(shown, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()[:32]


class WebMCPConnectionDecisionRequestV1(WebsiteContractV1):
    """Authenticated decision for one exact MCP connection proposal version."""

    context: GatewayRouteContextV1
    connection_id: str = Field(min_length=1, max_length=240)
    expected_record_digest: str = Field(min_length=64, max_length=64)
    decision: Literal["approve", "reject"]
    reason: str = Field(default="", max_length=4000)


class WebMCPConnectionDecisionResultV1(WebsiteContractV1):
    """Integrity-linked result of an MCP connection decision."""

    status: str = Field(min_length=1, max_length=80)
    connection_id: str = Field(min_length=1, max_length=240)
    request_digest: str = Field(min_length=64, max_length=64)
    record_digest: str = Field(min_length=64, max_length=64)
    response_digest: str = Field(min_length=64, max_length=64)
    decision: Literal["approve", "reject"]


class WebMCPLifecycleRequestV1(WebsiteContractV1):
    """Authenticated lifecycle control for one exact MCP resource version."""

    context: GatewayRouteContextV1
    resource_kind: Literal["connection", "hosted_server"]
    resource_id: str = Field(min_length=1, max_length=240)
    action: Literal["disable", "remove", "install", "start", "stop"]
    expected_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    expected_lifecycle: str = Field(min_length=1, max_length=80)


class WebMCPLifecycleResultV1(WebsiteContractV1):
    """Integrity-linked MCP lifecycle mutation result."""

    resource_kind: Literal["connection", "hosted_server"]
    resource_id: str
    action: str
    status: str
    request_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    result_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    recoverable: bool = True


@dataclass(frozen=True)
class GatewayWebChatIdentity:
    """Authenticated identity returned to the Website adapter."""

    auth_kind: str
    canonical_user_id: str | None
    display_name: str | None
    credential_kind: str = "bootstrap_token"
    enrollment_required: bool = False
    enrollment_label: str = ""


class IssuedWebEnrollmentTokenV1(WebsiteContractV1):
    """One-time browser token material returned during enrollment."""

    raw_token: str = Field(min_length=1, max_length=4096)
    token_id: str = Field(min_length=1, max_length=240)
    label: str = Field(min_length=1, max_length=240)


class ActivatedWebEnrollmentTokenV1(WebsiteContractV1):
    """Secret-free confirmation that a pending enrollment token is active."""

    token_id: str = Field(min_length=1, max_length=240)
    label: str = Field(min_length=1, max_length=240)


@dataclass(frozen=True)
class GatewayAttachmentContent:
    """Validated attachment metadata that the in-process Website may serve.

    ``storage_reference`` is the local path recorded when the file was staged
    or uploaded, and ``sha256`` its digest then; the download route serves the
    bytes only while they still match (``StagedAttachmentReader``).
    """

    attachment_id: str
    session_key: str
    storage_reference: str
    sha256: str
    filename: str
    content_type: str
    kind: str


@dataclass(frozen=True)
class GatewayGeneratedSiteSession:
    """Resolved session route for one generated-site chat request."""

    session_key: str
    preserve_channel_binding: bool


class GatewaySessionAliasConflict(ValueError):
    """Raised when a requested session alias is owned by another session."""


class WebChatParallelSettingResult(StrEnum):
    """What a change to a thread's Run concurrently switch did.

    The application's own copy of the store's ``ParallelSettingOutcome``:
    public contracts stay free of persistence types, as sessions are projected
    by ``gateway_session_view``. The switch is refused while a request is open
    in the thread's conversation (owner, 2026-10-29).
    """

    SAVED = "saved"
    REQUEST_OPEN = "request_open"
    CONVERSATION_CHANGED = "conversation_changed"
    NOT_FOUND = "not_found"


@runtime_checkable
class WebIdentityApplication(Protocol):
    """Authenticate browser identities and manage enrollment credentials."""

    def authenticate_web_chat_token(
        self,
        raw_token: str,
    ) -> GatewayWebChatIdentity | None:
        """Authenticate a configured bootstrap token or persisted user token."""

    def issue_web_user_enrollment_token(
        self,
        *,
        canonical_user_id: str,
        label: str,
        pending_expires_at_utc: str,
        replacement_token_id: str | None = None,
    ) -> IssuedWebEnrollmentTokenV1 | None:
        """Issue a pending reusable token for an invitation session."""

    def activate_web_user_enrollment_token(
        self,
        *,
        canonical_user_id: str,
        token_id: str,
    ) -> ActivatedWebEnrollmentTokenV1 | None:
        """Activate the pending token after the user confirms it was saved."""

    def revoke_pending_web_user_enrollment_token(
        self,
        *,
        canonical_user_id: str,
        token_id: str,
    ) -> bool:
        """Revoke an unconfirmed token when enrollment is abandoned."""

    def is_web_chat_user_active(self, canonical_user_id: str) -> bool:
        """Return whether a canonical user may continue using Web Chat."""

    def get_web_chat_appearance(
        self,
        canonical_user_id: str,
    ) -> WebAppearanceV1 | None:
        """Return one canonical user's saved appearance override."""

    def save_web_chat_appearance(
        self,
        *,
        canonical_user_id: str,
        appearance: WebAppearanceV1,
    ) -> WebAppearanceV1:
        """Persist one canonical user's validated appearance override."""

    def reset_web_chat_appearance(self, canonical_user_id: str) -> bool:
        """Remove one canonical user's appearance override."""


@runtime_checkable
class WebSessionApplication(Protocol):
    """Own Website session visibility, mutation, and attachment access."""

    def list_web_chat_threads(
        self,
        *,
        owner_canonical_user_id: str | None,
        limit: int = 50,
    ) -> list[WebChatThread]:
        """List the Web Chat threads an authenticated identity sees, named (web_chat_threads)."""

    def can_access_web_chat_session(
        self,
        *,
        session_key: str,
        owner_canonical_user_id: str | None,
        allow_missing: bool = False,
    ) -> bool:
        """Return whether an identity may access a Web Chat session reference."""

    def archive_web_chat_session(
        self,
        *,
        session_key: str,
        owner_canonical_user_id: str | None,
    ) -> bool:
        """Archive a Web Chat session when it is visible to the identity."""

    def rename_web_chat_session(
        self,
        *,
        session_key: str,
        session_alias: str,
        owner_canonical_user_id: str | None,
    ) -> GatewaySession | None:
        """Rename a visible Web Chat session or return ``None`` when absent."""

    def set_web_chat_session_parallel(
        self,
        *,
        session_key: str,
        conversation_id: str,
        parallel_thread: bool,
        owner_canonical_user_id: str | None,
    ) -> WebChatParallelSettingResult:
        """Save a visible thread's Run concurrently setting unless a request is open in it."""
        ...

    def web_chat_thread_states(
        self,
        *,
        owner_canonical_user_id: str | None,
    ) -> WebChatThreadStatesV1:
        """Return the live thread-list revision, busy threads and open requests for one owner."""

    def get_web_chat_attachment(
        self,
        *,
        attachment_id: str,
        owner_canonical_user_id: str | None,
        allow_missing_session: bool = False,
    ) -> GatewayAttachmentContent | None:
        """Return validated content metadata for an accessible attachment."""

    def resolve_generated_site_session(
        self,
        *,
        site_id: str,
        client_id: str,
        requested_session_key: str,
        conversation_id: str | None,
        owner_canonical_user_id: str,
    ) -> GatewayGeneratedSiteSession:
        """Resolve and, when needed, bind a generated-site chat session.

        Only sessions of ``owner_canonical_user_id`` (the page's signed-in
        owner) are used or created; another person's raises ``PermissionError``.
        """

    def get_session_details(self, session_reference: str) -> GatewaySession | None:
        """Return an application-facing session route without exposing storage."""

    def ensure_web_chat_session(
        self,
        *,
        session_key: str,
        canonical_user_id: str,
    ) -> GatewaySession:
        """Create or resolve the browser-owned session that Settings controls act in."""


@runtime_checkable
class WebTranscriptApplication(Protocol):
    """Own durable Web Chat transcript reads and edge-local exchanges."""

    def list_credential_actions(
        self, *, owner_canonical_user_id: str
    ) -> tuple[PendingCredentialAction, ...]:
        """Return live owner-scoped actions separately from durable event replay."""

    def snapshot_cursor(self) -> str:
        """Return the durable owner-wide high-water cursor for session hydration."""

    def list_messages(
        self,
        *,
        owner_canonical_user_id: str,
        session_key: str,
        before_cursor: str | None,
        limit: int,
    ) -> WebTranscriptHistoryPageV1 | None:
        """Return one visible session page or ``None`` when inaccessible."""

    def list_events(
        self,
        *,
        owner_canonical_user_id: str,
        after_cursor: str | None,
        limit: int,
    ) -> WebTranscriptEventPageV1:
        """Return owner-wide transcript changes after an opaque cursor."""

    def record_local_exchange(
        self,
        *,
        owner_canonical_user_id: str,
        session_key: str,
        conversation_id: str | None,
        client_request_id: str,
        user_text: str,
        response_text: str,
        response_message_type: str,
    ) -> tuple[WebTranscriptMessageV1, WebTranscriptMessageV1] | None:
        """Persist a gateway-local command exchange for one visible session."""

    def record_local_user_message(
        self,
        *,
        owner_canonical_user_id: str,
        session_key: str,
        conversation_id: str | None,
        client_request_id: str,
        user_text: str,
    ) -> tuple[WebTranscriptMessageV1, bool] | None:
        """Persist local input and report whether this caller owns execution."""

    def record_local_response(
        self,
        *,
        owner_canonical_user_id: str,
        session_key: str,
        client_request_id: str,
        response: GatewayApplicationResponse,
    ) -> WebTranscriptMessageV1 | None:
        """Persist the response paired with a previously accepted local message."""

    def get_exchange(
        self,
        *,
        owner_canonical_user_id: str,
        session_key: str,
        client_request_id: str,
    ) -> tuple[WebTranscriptMessageV1, WebTranscriptMessageV1 | None] | None:
        """Return the durable exchange associated with a client retry token."""


@runtime_checkable
class WebOperationsApplication(Protocol):
    """Expose Website operational views and controls."""

    def list_web_chat_team_members(self, *, presence_only: bool = False) -> WebTeamMemberListV1:
        """Return a redacted live-presence snapshot for the Settings Team view."""

    def list_web_chat_workspace_directory(
        self,
        *,
        canonical_user_id: str,
        roles: tuple[str, ...],
        path: str,
    ) -> WebWorkspaceDirectoryV1:
        """List one admin-authorized directory under the gateway workspace."""

    def inspect_web_chat_workspace_file(
        self,
        *,
        canonical_user_id: str,
        roles: tuple[str, ...],
        path: str,
    ) -> WebWorkspaceFileV1:
        """Return safe preview metadata and optional text for one file."""

    def read_web_chat_workspace_content(
        self,
        *,
        canonical_user_id: str,
        roles: tuple[str, ...],
        path: str,
    ) -> GatewayWorkspaceFileContent:
        """Return validated inline HTML, image, or PDF content."""

    async def list_web_chat_memories(
        self,
        request: WebMemoryListRequestV1,
    ) -> WebMemoryListV1:
        """Return one owner-checked active memory category."""

    async def mutate_web_chat_memory(
        self,
        request: WebMemoryMutationRequestV1,
    ) -> WebMemoryMutationResultV1:
        """Edit or delete one digest-bound memory when the action is allowed."""

    async def create_web_chat_memory(
        self,
        request: WebMemoryCreateRequestV1,
    ) -> WebMemoryMutationResultV1:
        """Create a fact through the manager's authorized memory command."""

    def list_web_chat_mcp_connections(
        self,
        request: WebMCPConnectionListRequestV1,
    ) -> WebMCPConnectionListV1:
        """Return secret-free installed MCP state for the Settings Team view."""

    def list_web_chat_hosted_mcp_servers(
        self,
        request: WebHostedMCPServerListRequestV1,
    ) -> WebHostedMCPServerListV1:
        """Return secret-free hosted MCP state for the Settings Team view."""

    def decide_web_chat_mcp_connection(
        self,
        request: WebMCPConnectionDecisionRequestV1,
    ) -> WebMCPConnectionDecisionResultV1:
        """Approve or reject one exact MCP connection proposal."""

    async def control_web_chat_mcp_lifecycle(
        self,
        request: WebMCPLifecycleRequestV1,
    ) -> WebMCPLifecycleResultV1:
        """Apply one exact authenticated MCP lifecycle control."""

    async def list_web_chat_tasks(self, request: WebTaskListRequestV1) -> WebTaskListV1:
        """List one bounded page of the owner's live TeamLead tasks."""

    async def control_web_chat_tasks(self, request: WebTaskControlRequestV1) -> WebTaskControlResultV1:
        """Cancel one task or purge queued work using the displayed inventory."""

    async def list_web_chat_objectives(
        self,
        context: GatewayRouteContextV1,
    ) -> WebObjectiveListV1:
        """List authoritative Objectives for one authenticated user."""

    async def control_web_chat_objective(
        self,
        request: WebObjectiveControlRequestV1,
    ) -> WebObjectiveControlResultV1:
        """Apply one exact Objective lifecycle action."""

    async def list_web_chat_schedules(
        self,
        context: GatewayRouteContextV1,
    ) -> WebScheduleListV1:
        """List owner-wide schedules for one authenticated user."""

    async def control_web_chat_schedule(
        self,
        request: WebScheduleControlRequestV1,
    ) -> WebScheduleControlResultV1:
        """Apply one exact schedule lifecycle action."""

    async def emit_delivery_event(self, event: GatewayDeliveryEvent) -> bool:
        """Emit a normalized delivery event through the configured output port."""

    async def send_context_debug_dump_control(
        self,
        *,
        enabled: bool,
        mode: str,
        session_key: str,
        conversation_id: str | None,
        channel_name: str,
        user_id: str,
        chat_id: str,
        thread_id: str | None,
        canonical_user_id: str | None,
    ) -> GatewayApplicationResponse:
        """Apply a manager-owned context-dump control for one Web Chat route."""


@runtime_checkable
class StatusApplication(Protocol):
    """Administrators' Status page (``assistant_gateway.status_page``): current findings, read only."""

    async def report(self) -> StatusReportV1:
        """Every finding from every source, and the sources that could not be asked."""


@runtime_checkable
class KnowledgeApplication(Protocol):
    """People's Knowledge page (``assistant_gateway.application.knowledge_pages``)."""

    @property
    def enabled(self) -> bool:
        """Whether the team's knowledge base is on."""

    async def run(
        self, context: GatewayRouteContextV1, payload: KnowledgeCommandPayload
    ) -> KnowledgeCommandResultPayload:
        """Run one request for the signed-in person, editors named; raises ``GatewayKnowledgeRefused`` when refused."""


@runtime_checkable
class AccountsApplication(Protocol):
    """The Settings Accounts tab (``assistant_gateway.account_sharing.GatewayAccountSharing``)."""

    def page(self, *, person_id: str, roles: tuple[str, ...]) -> AccountsPage:
        """Whose account agents use for each service, as the person sees it.

        Raises ``AccountsRefused`` when they are not an active member.
        """


@runtime_checkable
class FileRetentionApplication(Protocol):
    """Settings > Team > File Retention (``assistant_gateway.file_retention.GatewayFileRetention``)."""

    def page(self, *, person_id: str) -> FileRetentionPage:
        """The saved days and storage warning numbers, and whether this person may change them."""

    def save(
        self,
        *,
        person_id: str,
        upload_days: object,
        scratch_days: object,
        output_days: object,
        person_step_gb: object,
        nearing_limit_percent: object,
        nearly_full_percent: object,
    ) -> FileRetentionPage:
        """Save every number.

        Raises ``FileRetentionRefused``, ``FileRetentionInvalid`` or ``FileRetentionConflict``;
        nothing is saved then.
        """


class WebsiteGatewayApplication(GatewayApplication, Protocol):
    """Core channel use cases plus explicit Website-only capability facets."""

    @property
    def workflows(self) -> AccessWorkflowApplication:
        """Return owner-scoped workflow review operations."""

    @property
    def access_inbox(self) -> AccessInboxApplication:
        """Return access reviews and notices for the signed-in person."""

    @property
    def identity(self) -> WebIdentityApplication:
        """Return browser identity and enrollment operations."""

    @property
    def sessions(self) -> WebSessionApplication:
        """Return Website session and attachment operations."""

    @property
    def operations(self) -> WebOperationsApplication:
        """Return Website operational views and controls."""

    @property
    def transcripts(self) -> WebTranscriptApplication:
        """Return durable Web Chat transcript operations."""

    @property
    def procedures(self) -> ProcedureApplication:
        """Return installed-procedure reviews for administrators."""

    @property
    def status(self) -> StatusApplication:
        """Return administrators' Status page findings."""

    @property
    def knowledge(self) -> KnowledgeApplication:
        """Return people's Knowledge page requests."""

    @property
    def accounts(self) -> AccountsApplication:
        """Return the Settings Accounts tab: whose account agents use for each service."""

    @property
    def file_retention(self) -> FileRetentionApplication:
        """Return Settings > Team > File Retention: how long uploads and scratch files are kept."""
