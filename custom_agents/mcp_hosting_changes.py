"""Installing or removing a hosted MCP server as one exact, approved change.

A hosted server runs next to the runtime and offers the tools its declarative
definition describes. Installing it (activating its local connection, the team
tool ``mcp:<name>``) or removing it is a platform change (``hosted_mcp_server``
on ``fn:capabilities``), staged as an exact plan bound to the definition digest
and applied through ``apply_change`` (``access_control.changes``). Starting and
stopping an installed server are ordinary tool calls
(``hosted_mcp_servers`` with action start or stop) decided per call.

With authorization on, the plan an administrator approves also says that the
agent types the definition names may use all of the server's tools for
everyone's work, and applying it stores those grants
(``access_control.mcp_servers.everyone_grants``), as approving a server on the
gateway's Settings page does (access plan 7d). The Inbox decision is the
authority: only people who could give ``full`` on this platform change
(administrators) decide it. With authorization off, the named agent types
load the server at their next start.
"""

from __future__ import annotations

import logging
import re
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, field_validator

from access_control.changes import ChangeApproval
from custom_agents.access_runtime import acting_agent, runtime_access
from custom_agents.governance.authorized_operations import (
    FilePlanStore,
    GovernanceAuditEvent,
    atomic_write_json,
    model_digest,
)
from custom_agents.governance.runtime_adapters import LoggingGovernanceAuditSink
from custom_messages.contracts.access_v2 import ChangeApprovalMode, ChangeProposalV1, summary_line
from custom_messages.contracts.common_v1 import utc_now
from custom_messages.contracts.runtime_v1 import (
    AUTHORITY_MODE_AUTHORIZATION_DISABLED,
    AUTHORITY_MODE_PERSON_APPROVAL,
)
from id_generation import new_id
from mcp_control.hosting import HostedMCPLifecycle, MCPHostedServerDefinitionV1

logger = logging.getLogger(__name__)

HOSTED_MCP_SERVER_CHANGE_KIND = "hosted_mcp_server"
HOSTED_MCP_PLAN_SCHEMA_VERSION: Literal[1] = 1
HOSTED_MCP_PLAN_TTL_SECONDS = 24 * 60 * 60
HOSTED_MCP_CHANGE_ID_PREFIX = "mcp_host_change"
_CHANGE_ID_PATTERN = re.compile(r"mcp_host_change_(?:[0-9a-f]{12}|[0-9a-f]{20})")
HostedServerChange = Literal["install", "remove"]
_EXPECTED_STATE = {
    "install": (HostedMCPLifecycle.PENDING,),
    "remove": (HostedMCPLifecycle.PENDING, HostedMCPLifecycle.STOPPED),
}


class HostedMCPServerChangePlan(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal[1] = HOSTED_MCP_PLAN_SCHEMA_VERSION
    change_id: str
    plan_digest: str = ""
    created_at_utc: datetime
    expires_at_utc: datetime
    actor_principal_id: str
    actor_named_agent_type: str
    team_id: str
    owner_canonical_user_id: str
    source_task_id: str
    authority_mode: Literal["person_approval", "authorization_disabled_agreement"]
    operation: HostedServerChange
    hosted_server_id: str
    definition_digest: str
    server_name: str
    purpose: str
    tool_names: tuple[str, ...]
    target_agent_types: tuple[str, ...]
    reason: str

    @field_validator("change_id")
    @classmethod
    def _validate_change_id(cls, value: str) -> str:
        normalized = str(value or "").strip()
        if not _CHANGE_ID_PATTERN.fullmatch(normalized):
            raise ValueError("Invalid hosted MCP server change id.")
        return normalized


class HostedMCPServerChangeService:
    """Stage install/remove plans for the team lead and apply approved ones."""

    def __init__(self, agent: Any) -> None:
        self.agent = agent
        self.audit = LoggingGovernanceAuditSink()

    def stage(self, *, hosted_server_id: str, operation: str, reason: str) -> dict[str, Any]:
        normalized = str(operation or "").strip().lower()
        if normalized not in _EXPECTED_STATE:
            raise ValueError("A hosted MCP server change is install or remove.")
        actor = acting_agent(self.agent)
        if actor.agent.role != "team_lead" or not actor.person_id or not actor.task_id:
            raise PermissionError("The team lead stages hosted MCP server changes from a task for a person.")
        administration = self._administration()
        definition = administration.store.require(str(hosted_server_id or "").strip())
        self._require_changeable(definition, normalized, actor.agent.team_id)
        now = utc_now()
        plan = HostedMCPServerChangePlan(
            change_id=new_id(HOSTED_MCP_CHANGE_ID_PREFIX),
            created_at_utc=now,
            expires_at_utc=now + timedelta(seconds=HOSTED_MCP_PLAN_TTL_SECONDS),
            actor_principal_id=actor.agent.agent_id,
            actor_named_agent_type=actor.agent.agent_type,
            team_id=actor.agent.team_id,
            owner_canonical_user_id=actor.person_id,
            source_task_id=actor.task_id,
            authority_mode=self._authority_mode(),
            operation=normalized,
            hosted_server_id=definition.hosted_server_id,
            definition_digest=definition.definition_digest,
            server_name=definition.server_name,
            purpose=definition.purpose,
            tool_names=tuple(tool.name for tool in definition.tools),
            target_agent_types=tuple(definition.target_agent_types),
            reason=str(reason or "").strip() or f"{normalized.capitalize()} the hosted MCP server {definition.server_name}.",
        )
        plan = plan.model_copy(update={"plan_digest": model_digest(plan)})
        self._plan_store().save(plan)
        self.audit.record(
            GovernanceAuditEvent(
                operation=HOSTED_MCP_SERVER_CHANGE_KIND,
                phase="staged",
                change_id=plan.change_id,
                plan_digest=plan.plan_digest,
                task_id=plan.source_task_id,
                authority_mode=plan.authority_mode,
            )
        )
        return {
            **plan.model_dump(mode="json"),
            "next_step": (
                "Call apply_change(change_id, reason); a person approves this exact change in the Inbox."
                if plan.authority_mode == AUTHORITY_MODE_PERSON_APPROVAL
                else "Explain the change, get the person's agreement, then call "
                "apply_change(change_id, reason, confirmed=true)."
            ),
        }

    def load_plan(self, change_id: str) -> HostedMCPServerChangePlan:
        plan = self._plan_store().load(str(change_id or "").strip())
        actor = acting_agent(self.agent)
        if plan.plan_digest != model_digest(plan):
            raise PermissionError("Hosted MCP server change plan digest mismatch.")
        if plan.expires_at_utc <= utc_now():
            raise PermissionError("Hosted MCP server change plan has expired. Stage a fresh plan.")
        if plan.authority_mode != self._authority_mode():
            raise PermissionError("Authorization was turned on or off after planning. Stage a fresh plan.")
        if (
            plan.actor_principal_id != actor.agent.agent_id
            or plan.team_id != actor.agent.team_id
            or plan.owner_canonical_user_id != (actor.person_id or "")
            or plan.source_task_id != (actor.task_id or "")
        ):
            raise PermissionError("Hosted MCP server change plan belongs to another agent, person or task.")
        return plan

    def _verify_current(self, plan: HostedMCPServerChangePlan) -> None:
        """Not applied yet and the definition unchanged (before review and again when applying)."""

        if self._receipt_path(plan.change_id).is_file():
            raise FileExistsError(f"Hosted MCP server change {plan.change_id} was already applied.")
        definition = self._administration().store.require(plan.hosted_server_id)
        if definition.definition_digest != plan.definition_digest:
            raise PermissionError("The hosted MCP server changed after planning. Stage a fresh plan.")
        self._require_changeable(definition, plan.operation, plan.team_id)

    def describe(self, change_id: str) -> ChangeProposalV1:
        plan = self.load_plan(change_id)
        self._verify_current(plan)
        if plan.operation == "install":
            effect = (
                f"Installs the hosted MCP server {plan.server_name}: {plan.purpose}"[:1000],
                f"Its tools: {', '.join(plan.tool_names)}; they answer from reviewed, fixed behaviors."[:1000],
                f"Lets {', '.join(plan.target_agent_types)} use all of its tools (full access to "
                f"mcp:{plan.server_name}) for everyone's work once it is started."[:1000],
                f"Reason: {plan.reason}"[:1000],
            )
            title = f"Install the hosted MCP server {plan.server_name}"
            count = len(plan.tool_names)
            summary = (
                f"Install the hosted MCP server {plan.server_name} with {count} tool{'s' if count != 1 else ''} "
                f"for {', '.join(plan.target_agent_types)}. Its tools work once it is started."
            )
        else:
            effect = (
                f"Removes the hosted MCP server {plan.server_name}; its definition is kept in a recoverable folder.",
                "Agents lose its tools.",
                f"Reason: {plan.reason}"[:1000],
            )
            title = f"Remove the hosted MCP server {plan.server_name}"
            summary = f"Remove the hosted MCP server {plan.server_name}; agents lose its tools now."
        return ChangeProposalV1(
            kind=HOSTED_MCP_SERVER_CHANGE_KIND,
            change_id=plan.change_id,
            plan_digest=plan.plan_digest,
            title=title,
            summary=summary_line(summary),
            effect=effect,
        )

    def apply(self, change_id: str, approval: ChangeApproval) -> dict[str, Any]:
        plan = self.load_plan(change_id)
        agreement = approval.mode is ChangeApprovalMode.AGREEMENT
        if agreement != (plan.authority_mode == AUTHORITY_MODE_AUTHORIZATION_DISABLED):
            raise PermissionError("This plan needs a person's approval, not agreement (or the reverse).")
        self._verify_current(plan)
        administration = self._administration()
        if plan.operation == "install":
            result = administration.install(plan.hosted_server_id, approval=approval.to_record())
            result["grant_ids"] = self._let_target_types_use(administration, result, approval)
        else:
            result = administration.remove(plan.hosted_server_id, by=approval.approved_by)
        receipt = {
            "schema_version": HOSTED_MCP_PLAN_SCHEMA_VERSION,
            "receipt_id": new_id("mcp_host_rcpt"),
            "change_id": plan.change_id,
            "plan_digest": plan.plan_digest,
            "applied_at_utc": utc_now().isoformat(),
            "operation": plan.operation,
            "hosted_server_id": plan.hosted_server_id,
            "approval": approval.record(),
        }
        atomic_write_json(self._receipt_path(plan.change_id), receipt, immutable=True)
        self.audit.record(
            GovernanceAuditEvent(
                operation=HOSTED_MCP_SERVER_CHANGE_KIND,
                phase="applied",
                change_id=plan.change_id,
                plan_digest=plan.plan_digest,
                task_id=plan.source_task_id,
                authority_mode=plan.authority_mode,
            )
        )
        return {**result, "authority": {"mode": plan.authority_mode, "approval": approval.record()}}

    def _let_target_types_use(self, administration: Any, result: dict[str, Any], approval: ChangeApproval) -> list[str]:
        """Store the grants the approved plan named (module docstring); none with authorization off."""

        if approval.mode is not ChangeApprovalMode.PERSON_APPROVAL:
            return []
        from access_control.mcp_servers import everyone_grants
        from custom_agents.access_runtime import runtime_requests
        from custom_messages.contracts.access_v2 import SubjectKind, SubjectRefV1

        connection = administration.connection_store.require(str(result["connection_id"]))
        _ = runtime_access(self.agent).catalog  # reload: the catalog now includes the installed server
        service = runtime_requests(self.agent).service
        now = service.clock()
        grants = everyone_grants(
            team_id=service.team_id,
            tool_id=connection.descriptor.capability_id,
            target_agent_types=connection.target_agent_types,
            approved_by=approval.approved_by,
            approval_id=approval.request_id,
            now=now,
        )
        # One entry per tool (access_control.grant_entries): nothing new where
        # an agent type already holds the server.
        plan = service.plan_grants(list(grants))
        service.store.apply_together(
            service.team_id,
            grants=plan.new,
            replaced=plan.replaced,
            actor=SubjectRefV1(kind=SubjectKind.USER, id=approval.approved_by),
            at=now,
        )
        grants = plan.new + list(plan.covered.values())
        logger.warning(
            "An approved hosted MCP server install let its agents use it for everyone's work. "
            "event=mcp_server_access_given tool_id=%s approved_by=%s request_id=%s grants=%s",
            connection.descriptor.capability_id,
            approval.approved_by,
            approval.request_id,
            ",".join(grant.grant_id for grant in grants),
        )
        return [grant.grant_id for grant in grants]

    # -- helpers -------------------------------------------------------------------

    @staticmethod
    def _require_changeable(definition: MCPHostedServerDefinitionV1, operation: str, team_id: str) -> None:
        if definition.team_id != team_id:
            raise PermissionError("This hosted MCP server belongs to another team.")
        if definition.lifecycle not in _EXPECTED_STATE[operation]:
            done = "installed" if operation == "install" else "removed"
            raise PermissionError(
                f"The hosted MCP server is {definition.lifecycle.value}; it cannot be {done} now"
                + (" (stop it first)." if definition.lifecycle == HostedMCPLifecycle.RUNNING else ".")
            )

    def _authority_mode(self) -> str:
        return (
            AUTHORITY_MODE_PERSON_APPROVAL
            if runtime_access(self.agent).enabled
            else AUTHORITY_MODE_AUTHORIZATION_DISABLED
        )

    def _config(self) -> Any:
        from assistant_gateway.config import GatewayInstanceConfig

        configured = str(getattr(self.agent, "instance_config_path", "") or "").strip()
        if not configured:
            raise ValueError("No instance_config.yaml path is available for hosted MCP servers.")
        return GatewayInstanceConfig.load_for_process(configured)

    def _administration(self) -> Any:
        from custom_agents.providers.mcp_hosting import MCPHostingAdministration

        config = self._config()
        if not config.mcp.hosting.enabled:
            raise PermissionError("Declarative MCP hosting is disabled by operator policy.")
        return MCPHostingAdministration(config)

    def _root(self) -> Path:
        return self._config().instance_root_path / "runtime"

    def _plan_store(self) -> FilePlanStore[HostedMCPServerChangePlan]:
        path = self._root() / "mcp_host_change_plans"
        path.mkdir(parents=True, exist_ok=True)
        return FilePlanStore(
            path,
            HostedMCPServerChangePlan,
            change_id_pattern=_CHANGE_ID_PATTERN,
            invalid_change_id_message="Invalid hosted MCP server change id.",
            missing_plan_message="Hosted MCP server change plan not found: {change_id}",
        )

    def _receipt_path(self, change_id: str) -> Path:
        normalized = str(change_id or "").strip()
        if not _CHANGE_ID_PATTERN.fullmatch(normalized):
            raise ValueError("Invalid hosted MCP server change id.")
        path = self._root() / "mcp_host_change_receipts"
        path.mkdir(parents=True, exist_ok=True)
        return path / f"{normalized}.json"


__all__ = [
    "HOSTED_MCP_SERVER_CHANGE_KIND",
    "HostedMCPServerChangePlan",
    "HostedMCPServerChangeService",
]
