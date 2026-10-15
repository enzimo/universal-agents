"""People's MCP controls in the gateway (Settings → Team and the ``/mcp`` command).

A signed-in person may approve or reject a proposed MCP connection, disable
or remove an installed one, and install, start, stop or remove a hosted MCP
server — when they could give ``full`` on that change kind (administrators
with authorization on; any active team member with it off).
``PersonChangeCheck`` decides that from the person's current team roles, and
the same check decides which buttons the lists show. Approving a connection
is decided like installing a new web API tool (the ``access_proposal`` kind):
installing a server is a team decision. The decision is recorded as that
person's approval by the MCP administration services.

With authorization on, approving a server (a connection, or installing a
hosted server) also lets the agent types its record names use all of its
tools for everyone's work (``access_control.mcp_servers.everyone_grants``):
the approver is an administrator, so they may give agents access for
everyone's work. A server for every agent type (``*``) is given to the whole
team. Without that, an installed server would be a tool nobody may use until
someone asked again. Grants are checked against the approver's authority
(``AccessService.grant_refusal``); if they cannot be stored, an approved
connection goes back to pending. Per-agent-type attachment records no longer
exist (access plan 7d), so there is no "detach": people revoke grants on the
Access page instead.

Every change names the exact record it was shown (digest and lifecycle), so
a change made meanwhile refuses instead of acting on something the person did
not see. Lists are secret-free: endpoints, commands and credential modes, but
never credentials.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any

from access_control.mcp_servers import everyone_grants
from access_control.requests import ACCESS_PROPOSAL_CHANGE, AccessRequests
from assistant_gateway.access_setup import PersonChangeCheck
from assistant_gateway.application.route_context import GatewayRouteContextV1, SignedInPerson
from assistant_gateway.application.website_application import (
    WebHostedMCPServerListRequestV1,
    WebHostedMCPServerListV1,
    WebHostedMCPServerV1,
    WebMCPConnectionDecisionRequestV1,
    WebMCPConnectionDecisionResultV1,
    WebMCPConnectionListRequestV1,
    WebMCPConnectionListV1,
    WebMCPConnectionV1,
    WebMCPLifecycleRequestV1,
    WebMCPLifecycleResultV1,
)
from custom_agents.mcp_hosting_changes import HOSTED_MCP_SERVER_CHANGE_KIND
from custom_agents.providers.mcp_connections import MCPConnectionAdministration
from custom_agents.providers.mcp_hosting import MCPHostingAdministration
from custom_messages.contracts.access_v2 import SubjectKind, SubjectRefV1
from custom_messages.contracts.common_v1 import contract_sha256
from mcp_control.registry import MCPConnectionLifecycle, MCPConnectionRecordV1

logger = logging.getLogger(__name__)

# Deciding a proposed MCP connection is installing a team tool.
MCP_CONNECTION_DECISION_KIND = ACCESS_PROPOSAL_CHANGE
# Which actions a connection's lifecycle allows.
_CONNECTION_ACTIONS = {
    MCPConnectionLifecycle.ACTIVE: ("disable",),
    MCPConnectionLifecycle.DISABLED: ("remove",),
}
# Which hosted-server actions a lifecycle allows.
_HOSTED_ACTIONS = {
    "pending": ("install", "remove"),
    "stopped": ("start", "remove"),
    "running": ("stop",),
}


class GatewayMCPControls:
    def __init__(
        self,
        config: Any,
        *,
        person_changes: PersonChangeCheck,
        signed_in: SignedInPerson,
        access_requests: Callable[[], AccessRequests] | None = None,
    ) -> None:
        self._config = config
        self._changes = person_changes
        self._signed_in = signed_in
        # The gateway's access service (the Inbox's): stores the grants an
        # approval gives when authorization is on.
        self._access_requests = access_requests

    def _may_change(self, person_id: str | None, kind: str) -> bool:
        return bool(person_id) and self._changes.may_change(person_id, kind)

    # -- lists -----------------------------------------------------------------

    @staticmethod
    def _connection_controls(record: MCPConnectionRecordV1 | None, may_change: bool) -> dict[str, Any]:
        """The buttons one connection row shows to this person."""

        if record is None or not may_change:
            return {"can_decide": False, "available_actions": []}
        return {
            "can_decide": MCPConnectionAdministration.is_pending(record),
            "available_actions": list(_CONNECTION_ACTIONS.get(record.lifecycle, ())),
        }

    def _connections(self, person_id: str | None) -> list[dict[str, Any]]:
        administration = MCPConnectionAdministration.for_instance_config(self._config)
        records = {record.connection_id: record for record in administration.store.list_records()}
        may_change = self._may_change(person_id, MCP_CONNECTION_DECISION_KIND)
        public = administration.list_public()
        for item in public:
            item.update(self._connection_controls(records.get(str(item.get("connection_id") or "")), may_change))
        return public

    def _hosted_servers(self, person_id: str | None) -> list[dict[str, Any]]:
        administration = MCPHostingAdministration(self._config)
        definitions = {item.hosted_server_id: item for item in administration.store.list_definitions()}
        may_change = self._may_change(person_id, HOSTED_MCP_SERVER_CHANGE_KIND)
        public = administration.list_public()
        for item in public:
            definition = definitions.get(str(item.get("hosted_server_id") or ""))
            actions: list[str] = []
            if definition is not None and may_change:
                actions = list(_HOSTED_ACTIONS.get(definition.lifecycle.value, ()))
            item["available_actions"] = actions
        return public

    def list_web_chat_mcp_connections(self, request: WebMCPConnectionListRequestV1) -> WebMCPConnectionListV1:
        """Secret-free MCP connections, with the controls this person may use."""

        person_id = self._signed_in.person_or_none(request.context)
        return WebMCPConnectionListV1(
            connections=tuple(WebMCPConnectionV1.model_validate(item) for item in self._connections(person_id))
        )

    def list_web_chat_hosted_mcp_servers(self, request: WebHostedMCPServerListRequestV1) -> WebHostedMCPServerListV1:
        """Secret-free hosted MCP servers, with the controls this person may use."""

        person_id = self._signed_in.person_or_none(request.context)
        return WebHostedMCPServerListV1(
            servers=tuple(WebHostedMCPServerV1.model_validate(item) for item in self._hosted_servers(person_id))
        )

    # -- changes ---------------------------------------------------------------

    def _person(self, context: GatewayRouteContextV1, kind: str) -> str:
        return self._changes.require(self._signed_in.person_id(context), kind)

    def _let_target_types_use(self, record: MCPConnectionRecordV1, person_id: str) -> tuple[str, ...]:
        """With authorization on: the record's agent types may use the server for everyone's work."""

        if not self._config.authorization.enabled or self._access_requests is None:
            return ()
        service = self._access_requests().service  # the catalog now includes the installed server
        person = self._changes.person(person_id)
        if person is None:
            raise PermissionError("Only an active team member can approve an MCP server.")
        now = service.clock()
        grants = everyone_grants(
            team_id=service.team_id,
            tool_id=record.descriptor.capability_id,
            target_agent_types=record.target_agent_types,
            approved_by=person_id,
            approval_id=None,
            now=now,
        )
        for grant in grants:
            refusal = service.grant_refusal(person, grant)
            if refusal:
                raise PermissionError(refusal)
        # One entry per tool (access_control.grant_entries): nothing new where
        # an agent type already holds the server.
        plan = service.plan_grants(list(grants))
        service.store.apply_together(
            service.team_id,
            grants=plan.new,
            replaced=plan.replaced,
            actor=SubjectRefV1(kind=SubjectKind.USER, id=person_id),
            at=now,
        )
        grants = plan.new + list(plan.covered.values())
        logger.warning(
            "An administrator let agents use an installed MCP server for everyone's work. "
            "event=mcp_server_access_given tool_id=%s connection_id=%s by=%s grants=%s",
            record.descriptor.capability_id,
            record.connection_id,
            person_id,
            ",".join(grant.grant_id for grant in grants),
        )
        return tuple(grant.grant_id for grant in grants)

    def _approve(self, administration: MCPConnectionAdministration, pending: MCPConnectionRecordV1, person: str) -> MCPConnectionRecordV1:
        """Install the connection and let its agent types use it; back to pending if that fails."""

        record = administration.approve_by_person(pending.connection_id, person_id=person)
        try:
            self._let_target_types_use(record, person)
        except Exception:
            administration.store.save(pending)
            logger.warning(
                "An approved MCP connection went back to pending because its agents could not be given access. "
                "event=mcp_connection_approval_undone connection_id=%s by=%s",
                pending.connection_id,
                person,
                exc_info=True,
            )
            raise
        return record

    def decide_web_chat_mcp_connection(
        self, request: WebMCPConnectionDecisionRequestV1
    ) -> WebMCPConnectionDecisionResultV1:
        """Approve or reject one exact proposed connection as the signed-in person."""

        person = self._person(request.context, MCP_CONNECTION_DECISION_KIND)
        administration = MCPConnectionAdministration.for_instance_config(self._config)
        pending = administration.store.require(request.connection_id)
        expected_digest = str(request.expected_record_digest or "").strip().lower()
        if not expected_digest or pending.record_digest != expected_digest:
            raise PermissionError("MCP connection changed after the Settings presentation was rendered.")
        decision = str(request.decision or "").strip().lower()
        if decision == "approve":
            record = self._approve(administration, pending, person)
        elif decision == "reject":
            record = administration.reject_by_person(
                request.connection_id,
                person_id=person,
                reason=str(request.reason or "Rejected in Settings.").strip(),
            )
        else:
            raise ValueError("MCP connection decision must be approve or reject.")
        response_digest = contract_sha256(
            {
                "connection_id": record.connection_id,
                "request_record_digest": expected_digest,
                "result_record_digest": record.record_digest,
                "decision": decision,
                "status": record.lifecycle.value,
                "principal_id": person,
            }
        )
        return WebMCPConnectionDecisionResultV1.model_validate(
            {
                "status": record.lifecycle.value,
                "connection_id": record.connection_id,
                "request_digest": expected_digest,
                "record_digest": record.record_digest,
                "response_digest": response_digest,
                "decision": decision,
            }
        )

    async def _control_hosted(self, request: WebMCPLifecycleRequestV1) -> WebMCPLifecycleResultV1:
        person = self._person(request.context, HOSTED_MCP_SERVER_CHANGE_KIND)
        administration = MCPHostingAdministration(self._config)
        result = await administration.perform_by_person(
            hosted_server_id=request.resource_id,
            action=request.action,
            expected_definition_digest=request.expected_digest,
            expected_lifecycle=request.expected_lifecycle,
            person_id=person,
        )
        if request.action == "install":
            connection = administration.connection_store.require(str(result["connection_id"]))
            try:
                self._let_target_types_use(connection, person)
            except Exception as exc:
                raise PermissionError(
                    f"The hosted MCP server was installed, but its agents could not be given access ({exc}). "
                    "Give them access on the Access page."
                ) from exc
        return WebMCPLifecycleResultV1.model_validate({key: value for key, value in result.items() if key != "connection_id"})

    async def control_web_chat_mcp_lifecycle(self, request: WebMCPLifecycleRequestV1) -> WebMCPLifecycleResultV1:
        """Disable or remove a connection, or change a hosted server, as the signed-in person."""

        if request.resource_kind == "hosted_server":
            return await self._control_hosted(request)
        person = self._person(request.context, MCP_CONNECTION_DECISION_KIND)
        administration = MCPConnectionAdministration.for_instance_config(self._config)
        record = administration.store.require(request.resource_id)
        if record.record_digest != str(request.expected_digest or "").strip().lower():
            raise PermissionError("MCP connection changed after the control was rendered.")
        if record.lifecycle.value != str(request.expected_lifecycle or "").strip():
            raise PermissionError("MCP connection lifecycle changed after the control was rendered.")
        if request.action == "disable":
            changed = administration.disable_by_person(request.resource_id, person_id=person)
        elif request.action == "remove":
            changed = administration.remove_by_person(request.resource_id, person_id=person)
        else:
            raise ValueError("Unsupported MCP connection lifecycle action.")
        return WebMCPLifecycleResultV1.model_validate(
            {
                "resource_kind": "connection",
                "resource_id": changed.connection_id,
                "action": request.action,
                "status": changed.lifecycle.value,
                "request_digest": request.expected_digest,
                "result_digest": changed.record_digest,
                "recoverable": True,
            }
        )


__all__ = ["GatewayMCPControls", "MCP_CONNECTION_DECISION_KIND"]
