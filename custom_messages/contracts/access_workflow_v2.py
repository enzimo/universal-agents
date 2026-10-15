"""Access workflows: an agent's plan for a goal, shown as a view over access requests.

A workflow is a dependency map of steps that a team lead publishes before it
asks for anything, so the person the task works for sees the whole goal. Each
step names what it will need in the access model (``access_control``):

- ``change_kinds``: kinds of exact staged changes (the catalog's ``changes:``
  kinds, applied with ``apply_change``), such as ``agent_definition``;
- ``tool_access``: tools (or tool sets) with the level the step needs, such as
  ``mcp:crawl4ai`` at ``write``;
- ``tool_setup``: setup pages the person completes for a tool (a sign-in, an
  account link, or the team's sign-in app; ``ToolSetupRequestV1``), such as
  ``cli:github`` ``sign_in``.

Access requests (``AccessRequestV1``) made while a step runs are linked to it,
so the workflow page shows each request's current status from the access
store. Tool setup pages are linked the same way when TeamLead asks for them,
and their results update the step (``custom_agents.access_workflow_setups``). A workflow grants nothing: every request is decided by a person or by
the access service, and every call is decided again when it is made. Progress
is what the agent reports, not proof that an operation ran.

Schema version 2 replaced version 1's ``expected_actions`` (action ids of the
removed permission system); version 1 records are not read.
"""

from __future__ import annotations

from datetime import datetime, timezone
from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from custom_messages.contracts.credentials_v1 import ToolSetupKind, ToolSetupOwner

from custom_messages.contracts.access_v2 import (
    AccessLevel,
    AccessRequestV1,
    TargetKind,
    contract_digest,
)
from id_generation import new_id

WORKFLOW_SCHEMA_VERSION: Literal[2] = 2
WORKFLOW_RESOURCE = "AccessWorkflow"
WORKFLOW_PAGE_SIZE = 20
STEP_ID_PATTERN = r"^[a-z][a-z0-9_]{0,63}$"
MAX_STEP_REQUESTS = 20
MAX_UNASSIGNED_REQUESTS = 50
# A step names a catalog tool or tool set: fn:files, cli:google, mcp:crawl4ai, standard.
TOOL_ID_PATTERN = r"^[a-z_]+(:[A-Za-z0-9_.\-]+)?$"


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


class WorkflowModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)


class WorkflowPhase(StrEnum):
    READY = "ready"
    RUNNING = "running"
    WAITING = "waiting"
    FAILED = "failed"
    COMPLETE = "complete"
    CANCELLED = "cancelled"


class WorkflowStepState(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    WAITING = "waiting"
    COMPLETE = "complete"
    OMITTED = "omitted"
    FAILED = "failed"
    OUTCOME_UNKNOWN = "outcome_unknown"


# Steps that can still receive requests.
OPEN_STEP_STATES = frozenset({WorkflowStepState.RUNNING, WorkflowStepState.WAITING})
CLOSED_STEP_STATES = frozenset({WorkflowStepState.COMPLETE, WorkflowStepState.OMITTED})


class WorkflowToolAccessV2(WorkflowModel):
    """A tool or tool set a step needs, and the level it needs there."""

    tool_id: str = Field(min_length=1, max_length=240, pattern=TOOL_ID_PATTERN)
    level: AccessLevel


class WorkflowToolSetupV2(WorkflowModel):
    """A setup page a step expects the person (or an administrator) to complete for a tool."""

    tool_id: str = Field(min_length=1, max_length=240, pattern=TOOL_ID_PATTERN)
    kind: ToolSetupKind
    owner: ToolSetupOwner = "person"
    # For an external step: its id in the tool's catalog setup data.
    step_id: str | None = Field(default=None, max_length=40)

    @model_validator(mode="after")
    def _owner_fits_kind(self) -> WorkflowToolSetupV2:
        if self.kind == "sign_in" and self.owner != "person":
            raise ValueError(f"A sign-in is always the person's own ({self.tool_id}: owner person).")
        if self.kind == "team_sign_in_app" and self.owner != "team":
            raise ValueError(f"The team's sign-in app belongs to the team ({self.tool_id}: owner team).")
        if (self.kind == "external_step") != bool(self.step_id):
            raise ValueError(f"An external step names its step_id, and only an external step does ({self.tool_id}).")
        return self


# A setup page linked to a step, with how it ended (``waiting`` until the result arrives).
WorkflowSetupStatus = Literal["waiting", "completed", "declined", "expired", "unavailable"]


class WorkflowSetupLinkV2(WorkflowModel):
    """One tool setup page trusted code linked to a step, and its latest result."""

    setup_request_id: str = Field(min_length=1, max_length=160)
    tool_id: str = Field(min_length=1, max_length=240)
    kind: ToolSetupKind
    owner: ToolSetupOwner
    status: WorkflowSetupStatus = "waiting"
    attempt: int = Field(default=1, ge=0)
    account_label: str | None = Field(default=None, max_length=200)
    message: str = Field(default="", max_length=2000)
    from_task_id: str = Field(min_length=1, max_length=240)
    updated_at_utc: datetime = Field(default_factory=lambda: utc_now())
    # An external step: its catalog id and the approved title, for the page.
    step_id: str | None = Field(default=None, max_length=40)
    step_title: str = Field(default="", max_length=120)


class WorkflowStepProposalV2(WorkflowModel):
    step_id: str = Field(pattern=STEP_ID_PATTERN)
    title: str = Field(min_length=1, max_length=160)
    reason: str = Field(min_length=1, max_length=2000)
    kind: Literal["account", "capability", "setup", "work", "review"]
    depends_on: tuple[str, ...] = Field(default=(), max_length=64)
    optional: bool = False
    change_kinds: tuple[str, ...] = Field(default=(), max_length=20)
    tool_access: tuple[WorkflowToolAccessV2, ...] = Field(default=(), max_length=20)
    tool_setup: tuple[WorkflowToolSetupV2, ...] = Field(default=(), max_length=20)
    success_criterion: str = Field(min_length=1, max_length=2000)

    @model_validator(mode="after")
    def _expectations(self) -> WorkflowStepProposalV2:
        if len(set(self.change_kinds)) != len(self.change_kinds):
            raise ValueError(f"Step {self.step_id} lists a change kind twice.")
        if len({item.tool_id for item in self.tool_access}) != len(self.tool_access):
            raise ValueError(f"Step {self.step_id} lists a tool twice; give each tool once with the level it needs.")
        if len({(item.tool_id, item.kind) for item in self.tool_setup}) != len(self.tool_setup):
            raise ValueError(f"Step {self.step_id} lists a tool setup twice.")
        if self.kind == "account" and (self.change_kinds or self.tool_access):
            raise ValueError(
                f"Account step {self.step_id} needs no approval: the person signs in or links the account "
                "on a setup page (list it in tool_setup). Put the access that uses the account on a later step."
            )
        return self

    def covers_setup(self, tool_id: str, kind: str, owner: str, step_id: str | None = None) -> bool:
        """Whether this step expects a setup page of this kind for this tool and owner (and external step)."""

        return any(
            item.tool_id == tool_id and item.kind == kind and item.owner == owner and item.step_id == step_id
            for item in self.tool_setup
        )

    def covers(self, request: AccessRequestV1) -> bool:
        """Whether this step expects the request: the same change kind, or the tool at that level or lower."""

        if request.change is not None:
            return request.change.kind in self.change_kinds
        target = request.target
        for expected in self.tool_access:
            same_tool = (
                target.id == expected.tool_id
                if target.kind in {TargetKind.TOOL, TargetKind.TOOL_SET}
                else target.kind is TargetKind.OPERATION and target.id.startswith(f"{expected.tool_id}#")
            )
            if same_tool and expected.level.covers(request.level):
                return True
        return False


class WorkflowProposalV2(WorkflowModel):
    schema_version: Literal[2] = WORKFLOW_SCHEMA_VERSION
    title: str = Field(min_length=1, max_length=160)
    goal: str = Field(min_length=1, max_length=4000)
    target_agent_type: str = Field(min_length=1, max_length=240)
    steps: tuple[WorkflowStepProposalV2, ...] = Field(min_length=1, max_length=64)

    @model_validator(mode="after")
    def validate_graph(self) -> WorkflowProposalV2:
        by_id = {step.step_id: step for step in self.steps}
        if len(by_id) != len(self.steps):
            raise ValueError("Workflow step IDs must be unique.")
        visited: set[str] = set()
        visiting: set[str] = set()

        def visit(step_id: str) -> None:
            if step_id not in by_id:
                raise ValueError(f"Unknown workflow prerequisite {step_id!r}.")
            if step_id in visiting:
                raise ValueError("Workflow dependencies contain a cycle.")
            if step_id in visited:
                return
            visiting.add(step_id)
            step = by_id[step_id]
            if len(step.depends_on) != len(set(step.depends_on)):
                raise ValueError("Workflow dependencies must be unique.")
            for dependency in step.depends_on:
                visit(dependency)
            visiting.remove(step_id)
            visited.add(step_id)

        for step in self.steps:
            visit(step.step_id)
        if len(self.model_dump_json().encode()) > 65536:
            raise ValueError("Workflow proposal is too large.")
        return self


class WorkflowStepV2(WorkflowStepProposalV2):
    state: WorkflowStepState = WorkflowStepState.PENDING
    summary: str = Field(default="", max_length=4000)
    recovery: str = Field(default="", max_length=2000)
    # Access requests linked to this step when they were made (ids in the access store).
    request_ids: tuple[str, ...] = Field(default=(), max_length=MAX_STEP_REQUESTS)
    # Tool setup pages linked to this step, with their latest results.
    setups: tuple[WorkflowSetupLinkV2, ...] = Field(default=(), max_length=MAX_STEP_REQUESTS)
    # The person denied a request, or closed a setup page, for this step; it is
    # closed to new requests.
    blocked_by_person: bool = False
    updated_at_utc: datetime = Field(default_factory=utc_now)


class AccessWorkflowV2(WorkflowModel):
    schema_version: Literal[2] = WORKFLOW_SCHEMA_VERSION
    workflow_id: str = Field(default_factory=lambda: new_id("access_flow"))
    version: int = Field(default=1, ge=1)
    owner_person_id: str = Field(min_length=1, max_length=240)
    team_id: str = Field(min_length=1, max_length=240)
    requester_agent_id: str = Field(min_length=1, max_length=240)
    requester_agent_type: str = Field(min_length=1, max_length=240)
    source_task_id: str = Field(min_length=1, max_length=240)
    # The signed task context of the source task (access_control.identity).
    # Delegated child tasks carry the same signed context, so a worker's
    # request is shown on this workflow only when its verified context matches.
    source_context_id: str | None = Field(default=None, max_length=240)
    source_conversation_id: str | None = None
    source_correlation_id: str | None = None
    source_session_key: str | None = None
    idempotency_key: str = Field(min_length=1, max_length=240)
    proposal_digest: str
    target_agent_type: str
    title: str
    goal: str
    steps: tuple[WorkflowStepV2, ...]
    # Requests from this task tree that matched no single open step.
    unassigned_request_ids: tuple[str, ...] = Field(default=(), max_length=MAX_UNASSIGNED_REQUESTS)
    # Tool setup pages from this task tree that matched no single step.
    unassigned_setups: tuple[WorkflowSetupLinkV2, ...] = Field(default=(), max_length=MAX_UNASSIGNED_REQUESTS)
    phase: WorkflowPhase = WorkflowPhase.READY
    created_at_utc: datetime = Field(default_factory=utc_now)
    updated_at_utc: datetime = Field(default_factory=utc_now)

    @property
    def record_digest(self) -> str:
        return contract_digest(self.model_dump(mode="json"))

    @property
    def terminal(self) -> bool:
        return self.phase in {WorkflowPhase.COMPLETE, WorkflowPhase.CANCELLED}

    def step(self, step_id: str) -> WorkflowStepV2 | None:
        return next((item for item in self.steps if item.step_id == step_id), None)

    def linked_request_ids(self) -> list[str]:
        return [request_id for step in self.steps for request_id in step.request_ids]

    def has_setup(self, setup_request_id: str) -> bool:
        """Whether a setup page is already linked to a step or kept as not on a step."""

        linked = (link for step in self.steps for link in step.setups)
        return any(link.setup_request_id == setup_request_id for link in (*linked, *self.unassigned_setups))

    def incomplete_dependencies(self, step: WorkflowStepV2) -> list[str]:
        """The step's prerequisites that are not complete yet."""

        states = {item.step_id: item.state for item in self.steps}
        return [key for key in step.depends_on if states.get(key) != WorkflowStepState.COMPLETE]


class WorkflowEventV2(WorkflowModel):
    event_id: str = Field(default_factory=lambda: new_id("flow_event"))
    workflow_id: str
    sequence: int = 0
    step_id: str | None = None
    kind: Literal["progress", "decision", "error"]
    code: str = Field(min_length=1, max_length=160)
    message: str = Field(default="", max_length=4000)
    created_at_utc: datetime = Field(default_factory=utc_now)


class WorkflowProgressV2(WorkflowModel):
    step_id: str = Field(pattern=STEP_ID_PATTERN)
    state: WorkflowStepState
    summary: str = Field(min_length=1, max_length=4000)
    recovery: str = Field(default="", max_length=2000)

    @model_validator(mode="after")
    def failure_requires_recovery(self) -> WorkflowProgressV2:
        if self.state in {WorkflowStepState.FAILED, WorkflowStepState.OUTCOME_UNKNOWN} and not self.recovery:
            raise ValueError("Explain the failure and next recovery action.")
        return self


__all__ = [
    "AccessWorkflowV2",
    "CLOSED_STEP_STATES",
    "MAX_STEP_REQUESTS",
    "MAX_UNASSIGNED_REQUESTS",
    "OPEN_STEP_STATES",
    "STEP_ID_PATTERN",
    "WORKFLOW_PAGE_SIZE",
    "WORKFLOW_RESOURCE",
    "WORKFLOW_SCHEMA_VERSION",
    "WorkflowEventV2",
    "WorkflowModel",
    "WorkflowPhase",
    "WorkflowProgressV2",
    "WorkflowProposalV2",
    "WorkflowStepProposalV2",
    "WorkflowStepState",
    "WorkflowSetupLinkV2",
    "WorkflowSetupStatus",
    "WorkflowStepV2",
    "WorkflowToolAccessV2",
    "WorkflowToolSetupV2",
    "utc_now",
]
