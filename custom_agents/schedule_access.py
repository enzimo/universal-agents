"""Which access a scheduled worker needs and who still lacks it.

A scheduled job runs as its worker type for the schedule's owner. At run
time every tool call is decided for both (``access_control.decisions``: the
agent side and the person side), so an unattended run that lacks access
pauses for a person. Preparing a schedule therefore checks ahead of time, for
each declared requirement (a tool catalog id and one of its operations), the
catalog operation it needs and whether
the worker type and the owner already hold it as access that outlasts one task
(``task_id=None``). ``ScheduleWorkerService.authorize_access`` then hands on
what the team lead may give and names the rest for a person.

With authorization off every tool is available (decision D8), so there is
nothing to check and no check is built.
"""

from __future__ import annotations

import difflib
import logging
from dataclasses import dataclass
from typing import Any, Optional

from access_catalog.catalog import ToolCatalog
from access_control.decisions import AccessService, HandDownRefused
from access_control.entities import AgentPrincipal, PersonPrincipal, operation_id
from custom_messages.contracts.access_v2 import (
    OperationResolutionV1,
    TargetKind,
    TargetRefV1,
    ToolDefinitionV1,
)
from custom_messages.contracts.schedule_worker_v1 import AccessHolder

logger = logging.getLogger(__name__)

ACCESS_MISSING = "access_missing"
# The requirement's operation is not one of the tool's catalog operations.
NOT_IN_TOOL_CATALOG = "not_in_tool_catalog"
# The requirement names no catalog tool, for example a removed tool group id
# such as builtin:web_research; TeamLead re-saves the schedule's requirements.
REQUIREMENT_NOT_A_CATALOG_TOOL = "requirement_not_a_catalog_tool"
# The access check could not evaluate an operation that resolved a moment
# earlier: the tool catalog changed in between (an MCP server was removed).
TOOL_CATALOG_CHANGED = "tool_catalog_changed_during_check"
WORKER_TYPE: AccessHolder = "worker_type"
OWNER: AccessHolder = "owner"
# Command-table tools (gog, gh) list every command as command:<path> beside the
# broker's named actions; the named ones are what a requirement should use.
COMMAND_OPERATION_PREFIX = "command:"
SUGGESTION_LIMIT = 12


class RequirementProblem(ValueError):
    """A requirement the checks cannot use, with what to name instead.

    ``str()`` is the reason code the readiness check reports; ``detail`` says
    in plain words what is wrong and ``suggestions`` lists valid names.
    """

    def __init__(self, code: str, detail: str, suggestions: tuple[str, ...] = ()) -> None:
        super().__init__(code)
        self.code = code
        self.detail = detail
        self.suggestions = suggestions


def operation_suggestions(tool: ToolDefinitionV1, requested: str) -> tuple[str, ...]:
    """Valid operations to offer instead of ``requested``.

    Close spellings come first; named operations (``gmail_search``) come before
    command paths (``command:gmail.search``), which appear only when they are
    close matches or the tool has no named operations.
    """

    names = [operation.name for operation in tool.operations]
    named = [name for name in names if not name.startswith(COMMAND_OPERATION_PREFIX)]
    close = difflib.get_close_matches(requested, names, n=SUGGESTION_LIMIT, cutoff=0.6)
    close.sort(key=lambda name: name.startswith(COMMAND_OPERATION_PREFIX))
    rest = [name for name in (named or names) if name not in close]
    return tuple([*close, *rest][:SUGGESTION_LIMIT])


def _unknown_operation(tool: ToolDefinitionV1, requested: str) -> RequirementProblem:
    suggestions = operation_suggestions(tool, requested)
    detail = (
        f"{tool.tool_id} has no operation {requested!r}. A requirement names one of the tool's "
        "operations, not the function an agent calls to use it"
        f"{' (secure_cli runs command-line tools)' if requested == 'secure_cli' else ''}. "
        f"Its operations include: {', '.join(suggestions)}. "
        f'find_tools("{tool.tool_id}") lists them all with their levels.'
    )
    return RequirementProblem(NOT_IN_TOOL_CATALOG, detail, suggestions)


def _unknown_tool(catalog: ToolCatalog, tool_id: str) -> RequirementProblem:
    suggestions = tuple(difflib.get_close_matches(tool_id, list(catalog.tools), n=5, cutoff=0.5))
    detail = (
        f"{tool_id!r} is not a tool catalog id. A requirement's capability_id is a catalog id such as "
        "fn:web_research, cli:google or mcp:<server>; removed tool groups such as builtin:web_research "
        f"are not. {'Close ids: ' + ', '.join(suggestions) + '. ' if suggestions else ''}"
        "find_tools lists every tool id."
    )
    return RequirementProblem(REQUIREMENT_NOT_A_CATALOG_TOOL, detail, suggestions)


@dataclass(frozen=True)
class AccessNeed:
    """One requirement as a catalog operation, and who lacks it."""

    resolution: OperationResolutionV1
    missing_for: tuple[AccessHolder, ...]

    @property
    def target(self) -> TargetRefV1:
        return operation_target(self.resolution.tool_id, self.resolution.operation)


def operation_target(tool_id: str, operation: str) -> TargetRefV1:
    return TargetRefV1(kind=TargetKind.OPERATION, id=operation_id(tool_id, operation))


def requirement_resolution(catalog: ToolCatalog, tool_id: str, operation: str) -> OperationResolutionV1:
    """The catalog operation one requirement names; ``RequirementProblem`` when the catalog has none."""

    tool = catalog.tools.get(tool_id)
    if tool is None:
        raise _unknown_tool(catalog, tool_id)
    definition = tool.operation(operation)
    if definition is None:
        raise _unknown_operation(tool, operation)
    return OperationResolutionV1(
        tool_id=tool.tool_id, operation=definition.name, level=definition.level, source=definition.source
    )


@dataclass(frozen=True)
class ScheduleAccessCheck:
    """Checks one worker type and one owner against the access service."""

    service: AccessService
    worker: AgentPrincipal
    owner: PersonPrincipal

    @classmethod
    def for_agent(cls, agent: Any, *, worker_type: str, schedule_id: str) -> Optional["ScheduleAccessCheck"]:
        """The check for a worker type in one schedule's runs, for the person the agent's task works for.

        None with authorization off. The owner is the verified person of the
        task; callers have already checked the schedule belongs to them. The
        worker is checked as it runs, in that schedule, so the schedule's access
        (``SubjectKind.SCHEDULE``) counts and other schedules' does not.
        """

        from custom_agents.access_runtime import acting_agent, runtime_access

        access = runtime_access(agent)
        if not access.enabled or access.service is None:
            return None
        actor = acting_agent(agent)
        if actor.person is None:
            raise PermissionError("Scheduled worker access is checked from a task for the schedule's owner.")
        service = access.service
        # Refresh the catalog (installed MCP connections may have changed).
        _ = access.catalog
        worker = AgentPrincipal(
            worker_type, worker_type, service.agent_role_of(worker_type), service.team_id, schedule_id=schedule_id
        )
        return cls(service=service, worker=worker, owner=actor.person)

    def need(self, resolution: OperationResolutionV1) -> AccessNeed:
        target = operation_target(resolution.tool_id, resolution.operation)
        missing: list[AccessHolder] = []
        try:
            # The worker's access for the owner's work (decision D13).
            if not self.service.holds(
                self.worker, target, resolution.level, task_id=None, person_id=self.owner.person_id
            ):
                missing.append(WORKER_TYPE)
            if not self.service.holds(self.owner, target, resolution.level, task_id=None):
                missing.append(OWNER)
        except HandDownRefused as exc:
            # holds() refuses only a target missing from the service's catalog;
            # the requirement resolved against the catalog a moment earlier.
            logger.warning(
                "The access check could not evaluate a scheduled worker requirement. "
                "event=schedule_access_check_catalog_changed target=%s worker_type=%s reason=%s",
                target.id,
                self.worker.agent_type,
                exc,
            )
            raise RequirementProblem(
                TOOL_CATALOG_CHANGED,
                f"The access check could not evaluate {target.id}: {exc} The tool catalog changed while "
                "checking, for example an MCP server was removed. Run prepare_scheduled_job_access again; "
                "if it repeats, check the tool with find_tools.",
            ) from exc
        return AccessNeed(resolution=resolution, missing_for=tuple(missing))


__all__ = [
    "ACCESS_MISSING",
    "AccessNeed",
    "NOT_IN_TOOL_CATALOG",
    "OWNER",
    "REQUIREMENT_NOT_A_CATALOG_TOOL",
    "RequirementProblem",
    "ScheduleAccessCheck",
    "TOOL_CATALOG_CHANGED",
    "WORKER_TYPE",
    "operation_suggestions",
    "operation_target",
    "requirement_resolution",
]
