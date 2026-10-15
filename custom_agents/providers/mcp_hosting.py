"""Declarative MCP servers the runtime hosts: proposals and lifecycle.

An agent proposes an inert server definition (trusted behaviors and exact
JSON schemas only). Installing and removing one are platform changes
(``hosted_mcp_server``), approved by a person through ``apply_change``
(``custom_agents.mcp_hosting_changes``) or done by an administrator in the
gateway's MCP controls. Installing also activates the server's local MCP
connection, recording the same approval: it becomes the team tool
``mcp:<name>``, and the callers give the agent types the definition names
access to it (access plan 7d; one proposal is one decision). Starting and
stopping an installed server are ordinary tool calls decided by the per-call
access hook; the broker runs the server. Publishing outside the instance stays
unavailable until an operator registers trusted ingress.
"""

from __future__ import annotations

import logging
from typing import Any, Mapping, Sequence

from custom_agents.access_runtime import acting_agent
from custom_messages.contracts.access_v2 import ChangeApprovalRecordV1
from custom_messages.contracts.mcp_v1 import MCPHostLifecycleRequestV1
from mcp_control.hosting import (
    HostedMCPLifecycle,
    HostedMCPToolDefinitionV1,
    MCPHostedServerDefinitionV1,
    MCPHostedServerStore,
    resolve_mcp_hosting_paths,
)
from mcp_control.registry import (
    MCPConnectionLifecycle,
    MCPConnectionSource,
    MCPConnectionStore,
    MCPConnectionTransport,
    _new_record,
    descriptor_from_server_config,
    resolve_mcp_connection_paths,
)

LOGGER = logging.getLogger(__name__)
HOSTED_LIFECYCLE_OPERATIONS = ("start", "stop")


class MCPHostingAdministration:
    """Trusted lifecycle operations; callers have already been authorized."""

    def __init__(self, config: Any) -> None:
        self.config = config
        self.store = MCPHostedServerStore(resolve_mcp_hosting_paths(config))
        self.connection_store = MCPConnectionStore(
            resolve_mcp_connection_paths(config.instance_root_path)
        )

    def list_public(self) -> list[dict[str, Any]]:
        return [
            {
                "hosted_server_id": item.hosted_server_id,
                "definition_digest": item.definition_digest,
                "server_name": item.server_name,
                "purpose": item.purpose,
                "lifecycle": item.lifecycle.value,
                "target_agent_types": list(item.target_agent_types),
                "tools": [tool.name for tool in item.tools],
                "bind_host": item.bind_host,
                "port": item.port,
            }
            for item in self.store.list_definitions()
        ]

    def propose_by_agent(
        self,
        agent: Any,
        *,
        server_name: str,
        purpose: str,
        tools: Sequence[Mapping[str, Any]],
        target_agent_types: Sequence[str],
    ) -> MCPHostedServerDefinitionV1:
        if not self.config.mcp.hosting.enabled:
            raise PermissionError(
                "Declarative MCP hosting is disabled by operator policy."
            )
        actor = acting_agent(agent)
        if not actor.person_id:
            raise PermissionError("Propose hosted MCP servers from a task that works for a person.")
        normalized_name = str(server_name or "").strip().lower()
        existing = self.store.list_definitions()
        if any(item.server_name == normalized_name for item in existing):
            raise ValueError("A hosted MCP definition already uses this semantic name.")
        used_ports = {item.port for item in existing}
        port = next(
            (
                candidate
                for candidate in range(
                    self.config.mcp.hosting.port_range_start,
                    self.config.mcp.hosting.port_range_end + 1,
                )
                if candidate not in used_ports
            ),
            None,
        )
        if port is None:
            raise RuntimeError("No operator-reserved hosted MCP port is available.")
        definition = MCPHostedServerDefinitionV1.build(
            server_name=normalized_name,
            purpose=purpose,
            owner_canonical_user_id=actor.person_id,
            team_id=actor.agent.team_id,
            target_agent_types=tuple(sorted(set(target_agent_types))),
            tools=tuple(
                HostedMCPToolDefinitionV1.model_validate(item) for item in tools
            ),
            bind_host=self.config.mcp.hosting.bind_host,
            port=port,
            lifecycle=HostedMCPLifecycle.PENDING,
        )
        self.store.save(definition)
        LOGGER.info(
            "Agent proposed inert declarative MCP hosting. event=mcp_host_proposed "
            "hosted_server_id=%s agent_id=%s definition_digest=%s",
            definition.hosted_server_id,
            actor.agent.agent_id,
            definition.definition_digest,
        )
        return definition

    def _ensure_connection(
        self,
        definition: MCPHostedServerDefinitionV1,
    ) -> str:
        raw = {
            "name": definition.server_name,
            "transport": MCPConnectionTransport.STREAMABLE_HTTP.value,
            "server_url": f"http://{definition.bind_host}:{definition.port}/mcp",
            "tool_name_prefix": definition.server_name,
            "include_tools": [tool.name for tool in definition.tools],
        }
        descriptor = descriptor_from_server_config(
            raw,
            source=MCPConnectionSource.HOSTED,
            source_locator=f"hosted:{definition.hosted_server_id}",
            team_id=definition.team_id,
            owner_canonical_user_id=definition.owner_canonical_user_id,
        )
        existing = self.connection_store.find_by_source_digest(
            descriptor.source_config_sha256,
            source=MCPConnectionSource.HOSTED,
            source_locator=descriptor.source_locator,
        )
        if existing is not None:
            return str(existing.connection_id)
        record = _new_record(
            descriptor,
            lifecycle=MCPConnectionLifecycle.PENDING,
            target_agent_types=definition.target_agent_types,
        )
        self.connection_store.save(record)
        return str(record.connection_id)

    # -- trusted lifecycle operations ---------------------------------------------

    def install(self, hosted_server_id: str, *, approval: ChangeApprovalRecordV1) -> dict[str, Any]:
        """Install a pending definition and activate its local connection with the same approval.

        The caller lets the definition's agent types use the connection's tool
        (``access_control.mcp_servers.everyone_grants``) when authorization is on.
        """

        definition = self.store.require(hosted_server_id)
        if definition.lifecycle != HostedMCPLifecycle.PENDING:
            raise PermissionError("Only pending hosted definitions can be installed.")
        definition = self.store.update_lifecycle(
            definition.hosted_server_id,
            HostedMCPLifecycle.STOPPED,
            principal_id=approval.approved_by,
        )
        connection_id = self._ensure_connection(definition)
        connection = self.connection_store.require(connection_id)
        if connection.lifecycle == MCPConnectionLifecycle.PENDING:
            connection = self.connection_store.update_lifecycle(
                connection_id, lifecycle=MCPConnectionLifecycle.ACTIVE, approval=approval
            )
        LOGGER.info(
            "Hosted MCP server installed. event=mcp_host_installed hosted_server_id=%s "
            "connection_id=%s approval_mode=%s approved_by=%s",
            definition.hosted_server_id,
            connection_id,
            approval.mode.value,
            approval.approved_by,
        )
        return {
            "status": definition.lifecycle.value,
            "hosted_server_id": definition.hosted_server_id,
            "connection_id": connection_id,
            "tool_id": connection.descriptor.capability_id,
            "next_step": "Start it with hosted_mcp_servers(action='start'); its tools then appear "
            "on the next step of each agent that may use it.",
        }

    def remove(self, hosted_server_id: str, *, by: str) -> dict[str, Any]:
        definition = self.store.require(hosted_server_id)
        if definition.lifecycle == HostedMCPLifecycle.RUNNING:
            raise PermissionError("Stop the hosted MCP server before recoverable removal.")
        for connection in self.connection_store.list_records():
            if connection.descriptor.source_locator == f"hosted:{hosted_server_id}":
                # A disabled connection leaves the catalog, so grants on its
                # tool give nobody anything.
                self.connection_store.update_lifecycle(
                    connection.connection_id,
                    lifecycle=MCPConnectionLifecycle.DISABLED,
                )
        removed = self.store.remove_recoverably(hosted_server_id, principal_id=by)
        LOGGER.warning(
            "Hosted MCP server removed recoverably. event=mcp_host_removed hosted_server_id=%s by=%s",
            hosted_server_id,
            by,
        )
        return {"status": removed.lifecycle.value, "hosted_server_id": removed.hosted_server_id, "recoverable": True}

    async def change_running_state(self, hosted_server_id: str, operation: str, *, by: str) -> dict[str, Any]:
        """Start or stop an installed server through the broker, which runs it."""

        if operation not in HOSTED_LIFECYCLE_OPERATIONS:
            raise ValueError("Hosted MCP servers are started or stopped here; install and remove are changes.")
        definition = self.store.require(hosted_server_id)
        expected_state = HostedMCPLifecycle.STOPPED if operation == "start" else HostedMCPLifecycle.RUNNING
        if definition.lifecycle != expected_state:
            raise PermissionError(f"Hosted MCP server must be {expected_state.value} before {operation}.")
        from custom_agents.sdk.strands.mcp import secure_broker_connection
        import httpx

        broker_base_url, broker_token = secure_broker_connection()
        payload = MCPHostLifecycleRequestV1(definition_digest=definition.definition_digest)
        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.post(
                f"{broker_base_url}/v1/mcp/hosted/{definition.hosted_server_id}/{operation}",
                headers={"Authorization": f"Bearer {broker_token}"},
                json=payload.model_dump(mode="json", by_alias=True, exclude_none=True),
            )
        response.raise_for_status()
        definition = self.store.update_lifecycle(
            definition.hosted_server_id,
            HostedMCPLifecycle.RUNNING if operation == "start" else HostedMCPLifecycle.STOPPED,
            principal_id=by,
        )
        LOGGER.info(
            "Hosted MCP server %s. event=mcp_host_lifecycle_changed hosted_server_id=%s operation=%s by=%s",
            "started" if operation == "start" else "stopped",
            hosted_server_id,
            operation,
            by,
        )
        return {"status": definition.lifecycle.value, "hosted_server_id": definition.hosted_server_id, "broker": response.json()}

    async def perform_by_person(
        self,
        *,
        hosted_server_id: str,
        action: str,
        expected_definition_digest: str,
        expected_lifecycle: str,
        person_id: str,
    ) -> dict[str, Any]:
        """The gateway's MCP controls; the gateway checked the person through the access service."""

        definition = self.store.require(hosted_server_id)
        if definition.definition_digest != str(expected_definition_digest or ""):
            raise PermissionError("Hosted MCP definition changed after the control was rendered.")
        if definition.lifecycle.value != str(expected_lifecycle or "").strip():
            raise PermissionError("Hosted MCP lifecycle changed after the control was rendered.")
        if action == "install":
            from custom_agents.providers.mcp_connections import MCPConnectionAdministration

            result = self.install(hosted_server_id, approval=MCPConnectionAdministration.person_approval(person_id))
        elif action == "remove":
            result = self.remove(hosted_server_id, by=person_id)
        elif action in HOSTED_LIFECYCLE_OPERATIONS:
            result = await self.change_running_state(hosted_server_id, action, by=person_id)
        else:
            raise ValueError("Unsupported hosted MCP lifecycle action.")
        current = self.store.require(hosted_server_id)
        outcome = {
            "resource_kind": "hosted_server",
            "resource_id": current.hosted_server_id,
            "action": action,
            "status": result["status"],
            "request_digest": expected_definition_digest,
            "result_digest": current.definition_digest,
            "recoverable": True,
        }
        if "connection_id" in result:
            # The installed connection, so the gateway can give its agents access.
            outcome["connection_id"] = result["connection_id"]
        return outcome


__all__ = ["HOSTED_LIFECYCLE_OPERATIONS", "MCPHostingAdministration"]
