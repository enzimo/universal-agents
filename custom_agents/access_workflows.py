"""Access workflows: an agent's published plan, shown as a view over access requests.

The team lead publishes the whole goal as a dependency map before it asks for
anything (``submit``), reports progress on each step (``progress``), and the
person the task works for follows it on the workflow page. Each step names
what it needs in the access model: kinds of staged changes and tools with a
level (``custom_messages.contracts.access_workflow_v2``), checked against the
tool catalog when the plan is published.

Linking. ``WorkflowRequestWatcher`` is the ``RequestWatcher`` of the agent's
``AccessRequests`` (``custom_agents.access_runtime.runtime_requests``). When
an access or change request is stored, trusted code links it to the one open
(running or waiting) step whose expectation covers it: the same change kind,
or the same tool at the requested level or higher. It looks at workflows of
the request's own task and of tasks carrying the same verified signed task
context, which is how a delegated worker's request is shown on its parent's
step: every delegated task carries its root task's signed context unchanged,
so the lineage cannot be claimed by a message field. A request that matches
no single open step is kept on the workflow as "not on a step" and logged.
Tool setup pages (sign-ins, account links, the team's sign-in app) are linked
to steps that expect them the same way, and their results update the steps
(``custom_agents.access_workflow_setups``).

Denials. When a person denies a request linked to a step, the step is closed
("blocked by the person") the next time trusted code sees the decision: a new
request the step covers, a progress report, or the page. A new request that
only a blocked step covers is refused, so an agent cannot ask again
automatically. The access store stays the source of every request's status.

This module grants nothing and decides no access; every request is decided
by a person or the access service, and every call is decided again when made.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Callable, Iterable

from access_catalog.catalog import ToolCatalog
from access_control.account_sharing import TeamAccountSharing
from access_control.decisions import HandDownRefused
from access_control.ownership import private_access
from access_control.requests import ACCESS_PROPOSAL_CHANGE, LEVEL_WORDS
from custom_agents.access_workflow_diagnostics import EVENT_EXPLANATIONS, archived_task_evidence
from custom_agents.access_workflow_setups import (
    check_setup_expectations,
    setup_explanation,
    setup_view,
    setups_done,
)
from custom_messages.contracts.access_v2 import (
    AccessRequestKind,
    AccessRequestStatus,
    AccessRequestV1,
    TargetKind,
    contract_digest,
)
from custom_messages.contracts.access_workflow_v2 import (
    CLOSED_STEP_STATES,
    MAX_STEP_REQUESTS,
    MAX_UNASSIGNED_REQUESTS,
    OPEN_STEP_STATES,
    WORKFLOW_RESOURCE,
    AccessWorkflowV2,
    WorkflowEventV2,
    WorkflowPhase,
    WorkflowProgressV2,
    WorkflowProposalV2,
    WorkflowStepState,
    WorkflowStepV2,
    WorkflowToolAccessV2,
    utc_now,
)
from default_values import ACCESS_WORKFLOW_DEFAULTS
from storage.access_repository import AccessRepository
from storage.access_workflow_repository import (
    CORRECTION_EVENT_CODE,
    AccessWorkflowRepository,
    WorkflowConflict,
)
from storage.task_archive_repository import TaskArchiveRepository, create_task_archive_repository

logger = logging.getLogger(__name__)

# Change kinds that install an MCP server, so a plan may name its tool
# (mcp:<name>) before it is in the catalog: an access proposal (which installs
# servers proposed with mcp_connections(action='propose'), access plan 7d) and a hosted
# server's install.
MCP_INSTALL_CHANGE_KINDS = frozenset({ACCESS_PROPOSAL_CHANGE, "hosted_mcp_server"})
SAVE_ATTEMPTS = 3
# The batch tool asks people to approve changes, so it exists only while
# authorization is on (hidden otherwise by custom_agents.team_config_helpers).
WORKFLOW_BATCH_TOOL_NAME = "request_access_workflow_approvals"
AUTHORIZATION_ONLY_WORKFLOW_TOOL_NAMES = frozenset({WORKFLOW_BATCH_TOOL_NAME})
NOT_FOUND = "Workflow not found. Workflows saved before this release can no longer be shown; ask the agent to plan again."
# How the page and the agent read a request's status (from the access store).
OUTCOME_PENDING = "pending"
OUTCOME_APPROVED = "approved"  # granted; for a change, not applied yet
OUTCOME_APPLIED = "applied"  # an approved change that trusted code applied
OUTCOME_APPLY_FAILED = "apply_failed"
OUTCOME_DENIED = "denied"
OUTCOME_WITHDRAWN = "withdrawn"
OUTCOME_UNAVAILABLE = "unavailable"
SUCCESSFUL_OUTCOMES = frozenset({OUTCOME_APPROVED, OUTCOME_APPLIED})
ENDED_TASK_STATES = frozenset({"completed", "failed", "cancelled", "abandoned"})


@dataclass(frozen=True)
class WorkflowRequester:
    """The agent publishing or updating a workflow, from trusted runtime state.

    ``person_id`` and ``context_id`` come from the task's verified signed
    context (``access_control.identity``), never from message fields.
    """

    agent_id: str
    agent_type: str
    team_id: str
    person_id: str
    context_id: str | None = None


def check_expectations(steps: Iterable[Any], catalog: ToolCatalog, sharing: TeamAccountSharing) -> None:
    """Every change kind and tool a step names exists in the tool catalog, and its setups fit the team's sharing."""

    steps = list(steps)
    kinds = set(catalog.changes)
    targets = set(catalog.tools) | set(catalog.tool_sets)
    installs_mcp = any(kind in MCP_INSTALL_CHANGE_KINDS for step in steps for kind in step.change_kinds)
    for step in steps:
        unknown = sorted(set(step.change_kinds) - kinds)
        if unknown:
            raise ValueError(
                f"Step {step.step_id}: {', '.join(unknown)} is not a kind of change in the tool catalog. "
                f"Kinds of change: {', '.join(sorted(kinds))}."
            )
        for item in step.tool_access:
            if item.tool_id in targets or (item.tool_id.startswith("mcp:") and installs_mcp):
                continue
            raise ValueError(
                f"Step {step.step_id}: {item.tool_id} is not in the tool catalog; find_tools lists the tools. "
                "An MCP tool that is not installed yet can be named when another step installs its server "
                "(access_proposal or hosted_mcp_server)."
            )
    check_setup_expectations(steps, catalog, sharing)


def _request_title(request: AccessRequestV1) -> str:
    if request.change is not None:
        return request.change.title
    return f"{LEVEL_WORDS[request.level]} access to {request.target.id}"


def _outcome(request: AccessRequestV1) -> str:
    if request.status is AccessRequestStatus.WAITING:
        return OUTCOME_PENDING
    if request.status is AccessRequestStatus.DENIED:
        return OUTCOME_DENIED
    if request.status is AccessRequestStatus.WITHDRAWN:
        return OUTCOME_WITHDRAWN
    if request.kind is AccessRequestKind.CHANGE:
        if request.apply_error:
            return OUTCOME_APPLY_FAILED
        return OUTCOME_APPLIED if request.applied_at_utc is not None else OUTCOME_APPROVED
    return OUTCOME_APPROVED


def _step_explanation(state: str, blocked: bool, outcomes: list[str], dependencies: list[str]) -> tuple[str, str]:
    """A trusted code and plain explanation for one step (shown on the page and to the agent)."""

    if blocked:
        return (
            "blocked_by_person",
            "The person denied a request for this step. It grants nothing and will not be asked for again in this workflow.",
        )
    if any(outcome not in SUCCESSFUL_OUTCOMES | {OUTCOME_PENDING} for outcome in outcomes):
        return (
            "request_not_approved",
            "A request for this step was not approved, or an approved change failed to apply. Check each request.",
        )
    if OUTCOME_PENDING in outcomes:
        return ("waiting_for_person", "Waiting for a person to decide. Nothing is allowed until they do.")
    if dependencies:
        return ("prerequisites_incomplete", "Earlier required steps are not finished yet.")
    if state in {item.value for item in CLOSED_STEP_STATES}:
        return ("step_closed", "The agent reported this step as complete or left it out.")
    if state in {WorkflowStepState.FAILED.value, WorkflowStepState.OUTCOME_UNKNOWN.value}:
        return (
            "execution_needs_attention",
            "The step failed or its result is uncertain. Check existing results before trying again.",
        )
    return (
        "agent_work_remaining",
        "The agent still has to do or check this step and report the result. An approval alone does not finish it.",
    )


def review_label(step: dict[str, Any]) -> str | None:
    """A short status for a step's requests, counted over all of them."""

    requests = step["requests"]
    if not requests:
        return None
    if step["blocked_by_person"]:
        return "The person denied a request for this step"
    if step["unsuccessful_request_ids"]:
        return "A request needs attention; check each request's outcome"
    pending = len(step["pending_request_ids"])
    approved = sum(item["outcome"] in SUCCESSFUL_OUTCOMES for item in requests)
    if pending:
        return f"Waiting for {pending} decision(s); {approved} of {len(requests)} approved"
    return "All requests approved; the agent still has to finish and check the work"


class AccessWorkflowService:
    def __init__(
        self,
        repository: AccessWorkflowRepository,
        *,
        max_steps: int = ACCESS_WORKFLOW_DEFAULTS.max_steps.value,
        diagnostic_limit: int = ACCESS_WORKFLOW_DEFAULTS.diagnostic_event_limit.value,
    ):
        self.repository = repository
        self.max_steps = max_steps
        self.diagnostic_limit = diagnostic_limit
        self._access_store: AccessRepository | None = None
        self._task_archive: TaskArchiveRepository | None = None

    def access_store(self) -> AccessRepository:
        """The access store in the same database: the source of every request's status."""

        if self._access_store is None:
            self._access_store = AccessRepository(self.repository.settings)
        return self._access_store

    # -- ownership ----------------------------------------------------------------

    def require_owner(self, workflow_id: str, *, owner_id: str, team_id: str) -> AccessWorkflowV2:
        record = self.repository.get(workflow_id)
        if record is None or record.team_id != team_id:
            raise PermissionError(NOT_FOUND)
        decision = private_access(
            person_id=owner_id,
            owner_id=record.owner_person_id,
            resource_type=WORKFLOW_RESOURCE,
            resource_id=workflow_id,
            team_id=team_id,
        )
        if not decision.allowed:
            raise PermissionError(NOT_FOUND)
        return record

    def require_agent(self, workflow_id: str, *, requester: WorkflowRequester, task_id: str) -> AccessWorkflowV2:
        """The workflow, when this agent type publishes it from this task.

        A restarted agent has a new process id; the task and agent type
        survive restarts, and a different task cannot change the plan.
        """

        record = self.require_owner(workflow_id, owner_id=requester.person_id, team_id=requester.team_id)
        if record.source_task_id != task_id or record.requester_agent_type != requester.agent_type:
            raise PermissionError("Only the task that published this workflow may update it.")
        return record

    # -- the plan -----------------------------------------------------------------

    def submit(
        self,
        proposal: WorkflowProposalV2,
        *,
        requester: WorkflowRequester,
        task_id: str,
        idempotency_key: str,
        catalog: ToolCatalog,
        conversation_id: str | None = None,
        correlation_id: str | None = None,
        session_key: str | None = None,
    ) -> AccessWorkflowV2:
        if not requester.person_id or not requester.team_id or not task_id:
            raise PermissionError("A workflow is shown to the person a task works for; this task works for nobody.")
        if len(proposal.steps) > self.max_steps:
            raise ValueError(f"A workflow may have at most {self.max_steps} steps.")
        check_expectations(proposal.steps, catalog, TeamAccountSharing(self.access_store(), requester.team_id))
        existing = self.repository.for_task(task_id=task_id, team_id=requester.team_id)
        if any(item.idempotency_key != f"{task_id}:{idempotency_key}" for item in existing):
            raise WorkflowConflict("Continue the existing workflow for this task instead of publishing another one.")
        record = AccessWorkflowV2(
            owner_person_id=requester.person_id,
            team_id=requester.team_id,
            requester_agent_id=requester.agent_id,
            requester_agent_type=requester.agent_type,
            source_task_id=task_id,
            source_context_id=requester.context_id,
            source_conversation_id=conversation_id,
            source_correlation_id=correlation_id,
            source_session_key=session_key,
            idempotency_key=f"{task_id}:{idempotency_key}",
            proposal_digest=contract_digest(proposal.model_dump(mode="json")),
            target_agent_type=proposal.target_agent_type,
            title=proposal.title,
            goal=proposal.goal,
            steps=tuple(WorkflowStepV2(**step.model_dump()) for step in proposal.steps),
        )
        return self.repository.create(
            record,
            WorkflowEventV2(
                workflow_id=record.workflow_id,
                kind="progress",
                code="plan_submitted",
                message="The agent published the complete workflow.",
            ),
        )

    def revise_step(
        self,
        record: AccessWorkflowV2,
        *,
        step_id: str,
        change_kinds: list[str],
        tool_access: list[dict[str, Any]],
        reason: str,
        catalog: ToolCatalog,
    ) -> AccessWorkflowV2:
        """Correct what an unfinished step needs, before any request is linked to it.

        Cannot reopen a denied step, move the task, or erase results; requests
        are still decided one by one.
        """

        if record.terminal:
            raise WorkflowConflict("This workflow is closed.")
        step = record.step(step_id)
        if step is None:
            raise ValueError("Unknown workflow step.")
        self._check_denials(record, step)
        if not reason.strip():
            raise ValueError("Explain why the step needs something different.")
        changed = WorkflowStepV2.model_validate(
            {
                **step.model_dump(),
                "change_kinds": change_kinds,
                "tool_access": [WorkflowToolAccessV2.model_validate(item) for item in tool_access],
                "updated_at_utc": utc_now(),
            }
        )
        if not (changed.change_kinds or changed.tool_access):
            raise ValueError("Name at least one kind of change or tool the step needs.")
        steps = tuple(changed if item.step_id == step_id else item for item in record.steps)
        check_expectations(steps, catalog, TeamAccountSharing(self.access_store(), record.team_id))
        return self._save(
            record,
            steps=steps,
            revised_step_id=step_id,
            event=WorkflowEventV2(
                workflow_id=record.workflow_id,
                step_id=step_id,
                kind="progress",
                code=CORRECTION_EVENT_CODE,
                message=f"What this step needs was corrected: {reason.strip()}",
            ),
        )

    def progress(self, record: AccessWorkflowV2, update: WorkflowProgressV2) -> AccessWorkflowV2:
        """A step's progress as the agent reports it (``agent_reported_<state>``)."""

        return self._progress(record, update, code=f"agent_reported_{update.state.value}")

    def trusted_progress(self, record: AccessWorkflowV2, update: WorkflowProgressV2, *, code: str) -> AccessWorkflowV2:
        """A step's progress trusted code decided from evidence, under its own event code.

        ``set_up_tools`` runs mark access steps running when they ask
        (``access_requested``) and complete when the access is approved
        (``access_approved``); the page and the diagnostics must never show
        that as the agent's report. The same transition rules apply.
        """

        return self._progress(record, update, code=code)

    def _progress(self, record: AccessWorkflowV2, update: WorkflowProgressV2, *, code: str) -> AccessWorkflowV2:
        if record.terminal:
            raise WorkflowConflict("This workflow is closed.")
        step = record.step(update.step_id)
        if step is None:
            raise ValueError("Unknown workflow step.")
        self._check_denials(record, step)
        self._check_transition(record, step, update)
        changed = step.model_copy(update={**update.model_dump(exclude={"step_id"}), "updated_at_utc": utc_now()})
        steps = tuple(changed if item.step_id == step.step_id else item for item in record.steps)
        phase = self._phase(steps)
        return self._save(
            record,
            steps=steps,
            phase=phase,
            event=WorkflowEventV2(
                workflow_id=record.workflow_id,
                step_id=step.step_id,
                kind="error" if phase == WorkflowPhase.FAILED else "progress",
                code=code,
                message=update.summary,
            ),
        )

    def _check_transition(self, record: AccessWorkflowV2, step: WorkflowStepV2, update: WorkflowProgressV2) -> None:
        """Dependencies first, no silent reopening, and completion only after the step's requests are done."""

        if step.state == WorkflowStepState.OMITTED:
            raise WorkflowConflict("A step that was left out cannot resume automatically. Agree a new goal with the person.")
        if update.state == WorkflowStepState.OMITTED and not step.optional:
            raise ValueError("A required step cannot be left out.")
        if update.state == WorkflowStepState.COMPLETE and step.request_ids:
            self._require_requests_done(step)
        if update.state == WorkflowStepState.COMPLETE and not setups_done(step):
            raise WorkflowConflict(
                "A setup page this step expects is not finished yet; the step completes when the person has "
                "finished every one."
            )
        starting = update.state in {WorkflowStepState.RUNNING, WorkflowStepState.COMPLETE}
        if starting and record.incomplete_dependencies(step):
            raise WorkflowConflict("Complete this step's prerequisites first.")
        if step.state == WorkflowStepState.OUTCOME_UNKNOWN and update.state == WorkflowStepState.RUNNING:
            raise WorkflowConflict("Check the previous result before trying this step again.")

    def _require_requests_done(self, step: WorkflowStepV2) -> None:
        store = self.access_store()
        for request_id in step.request_ids:
            request = store.get_request(request_id)
            outcome = _outcome(request) if request is not None else OUTCOME_UNAVAILABLE
            if outcome == OUTCOME_APPROVED and request is not None and request.kind is AccessRequestKind.CHANGE:
                raise WorkflowConflict(
                    f"The approved change {request.change.change_id if request.change else ''} is not applied yet; "
                    "call apply_change for it before reporting the step complete."
                )
            if outcome not in SUCCESSFUL_OUTCOMES:
                raise WorkflowConflict(
                    f"Request {request_id} is {outcome}. Every request of a step must be approved "
                    "(and every change applied) before the step is complete."
                )

    @staticmethod
    def _phase(steps: tuple[WorkflowStepV2, ...]) -> WorkflowPhase:
        states = {step.state for step in steps}
        if states == {WorkflowStepState.OMITTED}:
            return WorkflowPhase.CANCELLED
        if states <= CLOSED_STEP_STATES:
            return WorkflowPhase.COMPLETE
        if states & {WorkflowStepState.FAILED, WorkflowStepState.OUTCOME_UNKNOWN}:
            return WorkflowPhase.FAILED
        if WorkflowStepState.WAITING in states:
            return WorkflowPhase.WAITING
        return WorkflowPhase.RUNNING

    def _save(
        self,
        record: AccessWorkflowV2,
        *,
        steps: tuple[WorkflowStepV2, ...] | None = None,
        phase: WorkflowPhase | None = None,
        event: WorkflowEventV2,
        revised_step_id: str | None = None,
        unassigned: tuple[str, ...] | None = None,
        unassigned_setups: tuple[Any, ...] | None = None,
    ) -> AccessWorkflowV2:
        changed = record.model_copy(
            update={
                "steps": steps if steps is not None else record.steps,
                "phase": phase if phase is not None else record.phase,
                "unassigned_request_ids": unassigned if unassigned is not None else record.unassigned_request_ids,
                "unassigned_setups": unassigned_setups if unassigned_setups is not None else record.unassigned_setups,
                "version": record.version + 1,
                "updated_at_utc": utc_now(),
            }
        )
        result = self.repository.save(
            changed, expected_version=record.version, events=(event,), revised_step_id=revised_step_id
        )
        logger.info(
            "Workflow updated. event=workflow_updated workflow_id=%s task_id=%s version=%s phase=%s code=%s",
            result.workflow_id,
            result.source_task_id,
            result.version,
            result.phase.value,
            event.code,
        )
        return result

    # -- requests on steps ------------------------------------------------------

    def workflows_for_request(self, request: AccessRequestV1, *, context_id: str | None) -> list[AccessWorkflowV2]:
        """Open workflows a request belongs to: its own task's, and those of its verified task context.

        Only workflows of the same team and the same person; the request's
        person comes from its task's signed context.
        """

        if not request.task_id or not request.person_id:
            return []
        records = {r.workflow_id: r for r in self.repository.for_task(task_id=request.task_id, team_id=request.team_id)}
        if context_id:
            for record in self.repository.for_context(context_id=context_id, team_id=request.team_id):
                records.setdefault(record.workflow_id, record)
        return [record for record in records.values() if record.owner_person_id == request.person_id]

    def check_request(self, request: AccessRequestV1, *, context_id: str | None) -> None:
        """Refuse a request that only steps the person denied cover (see the module docstring)."""

        for record in self.workflows_for_request(request, context_id=context_id):
            covering = [step for step in record.steps if step.covers(request)]
            closed = self.closed_steps(record, covering)
            usable = [step for step in covering if step.state in OPEN_STEP_STATES and step not in closed]
            if closed and not usable:
                logger.info(
                    "Refused a request for a workflow step the person denied. "
                    "event=workflow_request_refused_denied_step workflow_id=%s step_id=%s request_id=%s",
                    record.workflow_id,
                    closed[0].step_id,
                    request.request_id,
                )
                raise HandDownRefused(
                    f"The person denied a request for the step \"{closed[0].title}\" of the workflow "
                    f"\"{record.title}\". Do not ask again; tell the person what cannot happen, or agree a "
                    "different plan with them."
                )

    def closed_steps(self, record: AccessWorkflowV2, steps: list[WorkflowStepV2]) -> list[WorkflowStepV2]:
        """The given steps the person denied a request for; records a denial found only in the access store."""

        closed = []
        for step in steps:
            denied = None if step.blocked_by_person else self.denied_request_id(step)
            if denied is not None:
                self.record_denial(record.workflow_id, denied)
            if step.blocked_by_person or denied is not None:
                closed.append(step)
        return closed

    def link_request(self, request: AccessRequestV1, *, context_id: str | None) -> None:
        """Show a stored request on its open step (see the module docstring)."""

        for record in self.workflows_for_request(request, context_id=context_id):
            for attempt in range(SAVE_ATTEMPTS):
                try:
                    self._link_once(record.workflow_id, request)
                    break
                except WorkflowConflict:
                    if attempt == SAVE_ATTEMPTS - 1:
                        raise
                    logger.warning(
                        "Workflow changed while a request was linked; retrying. event=workflow_link_retry "
                        "workflow_id=%s request_id=%s",
                        record.workflow_id,
                        request.request_id,
                    )

    def _link_once(self, workflow_id: str, request: AccessRequestV1) -> None:
        record = self.repository.get(workflow_id)
        if record is None or record.terminal:
            return
        if request.request_id in record.linked_request_ids() or request.request_id in record.unassigned_request_ids:
            return
        candidates = [
            step
            for step in record.steps
            if step.state in OPEN_STEP_STATES and not step.blocked_by_person and step.covers(request)
        ]
        worker = request.task_id != record.source_task_id
        who = f"{request.requester_agent_type}'s request" if worker else "A request"
        if len(candidates) != 1 or len(candidates[0].request_ids) >= MAX_STEP_REQUESTS:
            self._keep_unassigned(record, request, candidates=len(candidates), who=who)
            return
        step = candidates[0]
        waiting = request.status is AccessRequestStatus.WAITING
        changed = step.model_copy(
            update={
                "request_ids": (*step.request_ids, request.request_id),
                "state": WorkflowStepState.WAITING if waiting else step.state,
                "updated_at_utc": utc_now(),
            }
        )
        steps = tuple(changed if item.step_id == step.step_id else item for item in record.steps)
        self._save(
            record,
            steps=steps,
            phase=self._phase(steps),
            event=WorkflowEventV2(
                workflow_id=record.workflow_id,
                step_id=step.step_id,
                kind="progress",
                code="request_waiting" if waiting else "request_granted_at_once",
                message=f"{who} for this step ({_request_title(request)}) "
                + ("waits for a person." if waiting else "was granted at once."),
            ),
        )

    def _keep_unassigned(self, record: AccessWorkflowV2, request: AccessRequestV1, *, candidates: int, who: str) -> None:
        # Steps name tools and staged changes, not folders, so a folder request is
        # never on a step; that is expected, not a mistake in the published plan.
        folder = request.target.kind is TargetKind.FOLDER
        (logger.info if folder else logger.warning)(
            "An access request matched no single open workflow step; it is shown as not on a step. "
            "event=workflow_request_unassigned workflow_id=%s request_id=%s candidates=%s folder=%s",
            record.workflow_id,
            request.request_id,
            candidates,
            folder,
        )
        why = (
            "steps name tools and changes, not folders"
            if folder
            else "several open steps expect it"
            if candidates > 1
            else "its step already has the most requests it can show"
            if candidates == 1
            else "no running step expects it"
        )
        unassigned = (*record.unassigned_request_ids, request.request_id)[-MAX_UNASSIGNED_REQUESTS:]
        self._save(
            record,
            unassigned=unassigned,
            event=WorkflowEventV2(
                workflow_id=record.workflow_id,
                kind="progress" if folder else "error",
                code="folder_request_not_on_step" if folder else "request_not_on_step",
                message=f"{who} ({_request_title(request)}) is not shown on a step: {why}. "
                "It is still decided in the Inbox as usual.",
            ),
        )

    def denied_request_id(self, step: WorkflowStepV2) -> str | None:
        """A linked request of the step that the person denied, read from the access store."""

        store = self.access_store()
        for request_id in step.request_ids:
            request = store.get_request(request_id)
            if request is not None and request.status is AccessRequestStatus.DENIED:
                return request_id
        return None

    def _check_denials(self, record: AccessWorkflowV2, step: WorkflowStepV2) -> None:
        """Honor a denial made anywhere (Inbox, chat, the workflow page) before accepting an update."""

        denied = self.denied_request_id(step) if not step.blocked_by_person else None
        if denied is not None:
            self.record_denial(record.workflow_id, denied)
        if step.blocked_by_person or denied is not None:
            raise WorkflowConflict(
                "The person denied a request or closed a setup page for this step. "
                "Do not ask for it again in this workflow."
            )

    def record_denial(self, workflow_id: str, request_id: str) -> AccessWorkflowV2:
        """Close the steps a denied request belongs to, so no model retry reopens them."""

        for attempt in range(SAVE_ATTEMPTS):
            try:
                return self._record_denial(workflow_id, request_id)
            except WorkflowConflict:
                if attempt == SAVE_ATTEMPTS - 1:
                    raise
                logger.warning(
                    "Workflow changed while a denial was recorded; retrying. event=workflow_denial_retry "
                    "workflow_id=%s request_id=%s",
                    workflow_id,
                    request_id,
                )
        raise AssertionError("unreachable")

    def _record_denial(self, workflow_id: str, request_id: str) -> AccessWorkflowV2:
        record = self.repository.get(workflow_id)
        if record is None:
            raise ValueError("Workflow is missing.")
        matched = [step for step in record.steps if request_id in step.request_ids]
        if not matched:
            raise ValueError("The request does not belong to this workflow.")
        if all(step.blocked_by_person for step in matched):
            return record
        steps = tuple(
            step.model_copy(
                update={
                    "blocked_by_person": True,
                    "state": WorkflowStepState.OMITTED if step.optional else WorkflowStepState.FAILED,
                    "summary": "The person denied a request for this step. It grants nothing.",
                    "recovery": "This step will not be asked for again in this workflow. Agree a different plan with the person.",
                    "updated_at_utc": utc_now(),
                }
            )
            if request_id in step.request_ids
            else step
            for step in record.steps
        )
        return self._save(
            record,
            steps=steps,
            phase=self._phase(steps),
            event=WorkflowEventV2(
                workflow_id=workflow_id,
                step_id=matched[0].step_id,
                kind="decision",
                code="step_denied",
                message="The person denied a request for this step; it is closed to new requests.",
            ),
        )

    # -- what the page and the agent read ---------------------------------------

    def request_view(self, record: AccessWorkflowV2, request_id: str) -> dict[str, Any]:
        """One linked request's current status from the access store (owner-matched)."""

        request = self.access_store().get_request(request_id)
        item: dict[str, Any] = {"request_id": request_id, "outcome": OUTCOME_UNAVAILABLE}
        if request is None or request.team_id != record.team_id or request.person_id != record.owner_person_id:
            return item
        item.update(
            {
                "outcome": _outcome(request),
                "kind": request.kind.value,
                "status": request.status.value,
                "title": _request_title(request),
                "agent_type": request.requester_agent_type,
                "from_worker": request.task_id != record.source_task_id,
                "target": request.target.id,
                "level": request.level.value,
                "change_id": request.change.change_id if request.change is not None else None,
                "decision": request.decision.value if request.decision else None,
                "created_at_utc": request.created_at_utc.isoformat(),
                "decided_at_utc": request.decided_at_utc.isoformat() if request.decided_at_utc else None,
            }
        )
        return item

    def evidence(self, record: AccessWorkflowV2) -> list[dict[str, Any]]:
        """Each step with its requests' current statuses; explains, never authorizes."""

        return [self._step_evidence(record, step) for step in record.steps]

    def _step_evidence(self, record: AccessWorkflowV2, step: WorkflowStepV2) -> dict[str, Any]:
        requests = [self.request_view(record, request_id) for request_id in step.request_ids]
        outcomes = [item["outcome"] for item in requests]
        dependencies = record.incomplete_dependencies(step)
        blocked = step.blocked_by_person or OUTCOME_DENIED in outcomes
        code, explanation = setup_explanation(step) or _step_explanation(
            step.state.value, blocked, outcomes, dependencies
        )
        unsuccessful = SUCCESSFUL_OUTCOMES | {OUTCOME_PENDING}
        return {
            "step_id": step.step_id,
            "state": step.state.value,
            "depends_on": list(step.depends_on),
            "incomplete_dependencies": dependencies,
            "change_kinds": list(step.change_kinds),
            "tool_access": [item.model_dump(mode="json") for item in step.tool_access],
            "tool_setup": [item.model_dump(mode="json") for item in step.tool_setup],
            "setups": [setup_view(link) for link in step.setups],
            "optional": step.optional,
            "blocked_by_person": blocked,
            "request_ids": list(step.request_ids),
            "requests": requests,
            "pending_request_ids": [i["request_id"] for i in requests if i["outcome"] == OUTCOME_PENDING],
            "unsuccessful_request_ids": [i["request_id"] for i in requests if i["outcome"] not in unsuccessful],
            "reason_code": code,
            "explanation": explanation,
            "updated_at_utc": step.updated_at_utc.isoformat(),
        }

    def unassigned_evidence(self, record: AccessWorkflowV2) -> list[dict[str, Any]]:
        return [self.request_view(record, request_id) for request_id in record.unassigned_request_ids]

    def task_status(self, record: AccessWorkflowV2) -> dict[str, Any] | None:
        """Show an ended source task even when the agent omitted its final update."""

        if self._task_archive is None:
            self._task_archive = create_task_archive_repository(self.repository.settings)
        archived = self._task_archive.get_archived_task(
            record.source_task_id,
            owner_agent_id=None,
            include_all_agents=True,
            owner_canonical_user_id=record.owner_person_id,
        )
        if archived is None or archived.team_id != record.team_id or archived.status not in ENDED_TASK_STATES:
            return None
        if record.phase == WorkflowPhase.COMPLETE and archived.status == "completed":
            return {"state": archived.status, "summary": "The source task has ended.", "recovery": ""}
        return {
            "state": archived.status,
            "recorded_at_utc": archived.timestamp,
            "reason_code": "source_task_ended_with_open_steps" if not record.terminal else "source_task_outcome_mismatch",
            "unfinished_step_ids": [s.step_id for s in record.steps if s.state not in CLOSED_STEP_STATES],
            "summary": (
                f"The source task {archived.status}, although the agent reported this workflow complete. Check the final result."
                if record.phase == WorkflowPhase.COMPLETE
                else f"The source task {archived.status} before all workflow steps were finished."
            ),
            "recovery": "Open Chat to go over the remaining work. Keep finished results and check uncertain ones "
            "before trying again. The diagnostic log includes the task reference.",
        }

    def diagnostics(self, record: AccessWorkflowV2) -> dict[str, Any]:
        """Allowlisted structural diagnostics, never free text from requests or tool output."""

        events = self.repository.events(record.workflow_id, limit=self.diagnostic_limit + 1, newest_first=True)
        task_status = self.task_status(record)
        task_history = None
        if task_status is not None and self._task_archive is not None:
            archived = self._task_archive.get_archived_task(
                record.source_task_id,
                owner_agent_id=None,
                include_all_agents=True,
                owner_canonical_user_id=record.owner_person_id,
            )
            if archived is not None and archived.team_id == record.team_id:
                task_history = archived_task_evidence(archived.task_json, limit=self.diagnostic_limit)
        return {
            "schema_version": 3,
            "workflow_id": record.workflow_id,
            "task_id": record.source_task_id,
            "version": record.version,
            "correlation_id": record.source_correlation_id,
            "proposal_digest": record.proposal_digest,
            "created_at_utc": record.created_at_utc.isoformat(),
            "updated_at_utc": record.updated_at_utc.isoformat(),
            "excluded": ["chat", "work content", "request reasons", "credentials", "broker URLs", "raw tool responses"],
            "phase": record.phase.value,
            "task_status": task_status,
            "source_task_history": task_history,
            "truncated": len(events) > self.diagnostic_limit,
            "steps": [_diagnostic_step(step) for step in self.evidence(record)],
            "requests_not_on_a_step": [_diagnostic_request(item) for item in self.unassigned_evidence(record)],
            "interpretation": {
                "approval_is_execution": False,
                "request_status_source": "the access store; every request is decided by a person or the access service",
                "task_status_source": "owner-scoped task archive; null means no ended task was found",
                "workflow_state_source": "agent progress reports, checked against prerequisites and linked request outcomes",
                "recovery": "Keep finished steps. Read each pending or unsuccessful request's outcome. "
                "Check uncertain external results before another attempt.",
            },
            "events": [
                {
                    **event.model_dump(mode="json", include={"sequence", "step_id", "kind", "code", "created_at_utc"}),
                    "explanation": EVENT_EXPLANATIONS.get(event.code, "See the saved step state and request outcomes."),
                }
                for event in reversed(events[: self.diagnostic_limit])
            ],
        }


_DIAGNOSTIC_REQUEST_FIELDS = ("request_id", "outcome", "kind", "status", "agent_type", "from_worker", "target", "level",
                              "change_id", "decision", "created_at_utc", "decided_at_utc")


def _diagnostic_request(item: dict[str, Any]) -> dict[str, Any]:
    """A request's structural facts only (no titles, which can carry plan text)."""

    return {key: item[key] for key in _DIAGNOSTIC_REQUEST_FIELDS if key in item}


def _diagnostic_step(step: dict[str, Any]) -> dict[str, Any]:
    return {**step, "requests": [_diagnostic_request(item) for item in step["requests"]]}


class WorkflowRequestWatcher:
    """The ``RequestWatcher`` an agent's ``AccessRequests`` uses (see the module docstring).

    ``context_id`` returns the verified signed context id of the task making
    the request; the caller verifies it (``custom_agents.access_runtime``).
    """

    def __init__(self, service: Callable[[], AccessWorkflowService], context_id: Callable[[], str | None]) -> None:
        self._service = service
        self._context_id = context_id

    def check(self, request: AccessRequestV1) -> None:
        self._service().check_request(request, context_id=self._context_id())

    def stored(self, request: AccessRequestV1) -> None:
        self._service().link_request(request, context_id=self._context_id())


def workflow_service_for(agent: Any) -> AccessWorkflowService:
    """The agent's workflow service over its task store (cached on the agent).

    Limits for publishing plans come from configuration in the workflow tools;
    linking needs none.
    """

    existing = getattr(agent, "_access_workflow_service", None)
    if isinstance(existing, AccessWorkflowService):
        return existing
    service = AccessWorkflowService(AccessWorkflowRepository(agent.task_manager.storage_settings))
    agent._access_workflow_service = service
    return service


__all__ = [
    "AUTHORIZATION_ONLY_WORKFLOW_TOOL_NAMES",
    "AccessWorkflowService",
    "MCP_INSTALL_CHANGE_KINDS",
    "WorkflowRequestWatcher",
    "WORKFLOW_BATCH_TOOL_NAME",
    "WorkflowRequester",
    "check_expectations",
    "review_label",
    "workflow_service_for",
]
