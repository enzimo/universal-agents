"""Publication bridge from healthy executable services to the reviewed MCP registry."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
import re

from mcp_control.registry import (
    MCPConnectionLifecycle,
    MCPConnectionSource,
    MCPConnectionStore,
    MCPConnectionTransport,
    _new_record,
    descriptor_from_server_config,
)
from runtime_hosting.execution_contracts import ManagedServiceV1, ServiceKind


EndpointResolver = Callable[[ManagedServiceV1], str]


@dataclass(frozen=True)
class ManagedMCPServicePublisher:
    """Creates pending MCP records; an administrator installs one (Settings, or an access proposal's MCP server item)."""

    connection_store: MCPConnectionStore
    endpoint_resolver: EndpointResolver

    def publish(self, service: ManagedServiceV1) -> None:
        if service.service_kind != ServiceKind.MCP_SERVER:
            return
        activation = service.activation
        # The person the approved service change was made for owns the connection.
        owner_user_id = (
            str(activation.holder.person_id or "").strip() if activation else ""
        )
        if not owner_user_id:
            raise PermissionError(
                "Executable MCP publication requires an exact canonical user owner."
            )
        source_locator = self._source_locator(service)
        raw = {
            "name": self._connection_name(service),
            "transport": MCPConnectionTransport.STREAMABLE_HTTP.value,
            "server_url": self.endpoint_resolver(service),
            "tool_name_prefix": self._connection_name(service),
            "include_tools": list(service.mcp_tool_names),
        }
        descriptor = descriptor_from_server_config(
            raw,
            source=MCPConnectionSource.HOSTED,
            source_locator=source_locator,
            team_id=service.owner.team_id,
            owner_canonical_user_id=owner_user_id,
        )
        existing = self.connection_store.find_by_source_digest(
            descriptor.source_config_sha256,
            source=MCPConnectionSource.HOSTED,
            source_locator=source_locator,
        )
        if existing is not None:
            return
        self.connection_store.save(
            _new_record(
                descriptor,
                lifecycle=MCPConnectionLifecycle.PENDING,
                target_agent_types=service.mcp_target_agent_types,
            )
        )

    def unpublish(self, service: ManagedServiceV1) -> None:
        if service.service_kind != ServiceKind.MCP_SERVER:
            return
        prefix = f"managed-service:{service.service_id}:"
        for record in self.connection_store.list_records():
            if (
                record.descriptor.source == MCPConnectionSource.HOSTED
                and record.descriptor.source_locator.startswith(prefix)
                and record.lifecycle
                not in {MCPConnectionLifecycle.DISABLED, MCPConnectionLifecycle.REMOVED}
            ):
                self.connection_store.update_lifecycle(
                    record.connection_id,
                    lifecycle=MCPConnectionLifecycle.DISABLED,
                )

    @staticmethod
    def _source_locator(service: ManagedServiceV1) -> str:
        return f"managed-service:{service.service_id}:{service.generation}"

    @staticmethod
    def _connection_name(service: ManagedServiceV1) -> str:
        normalized = re.sub(r"[^a-z0-9_.-]+", "-", service.name.lower()).strip("-._")
        if not normalized or not normalized[0].isalpha():
            normalized = f"service-{service.service_id}"
        return normalized[:120]


def kubernetes_mcp_endpoint(service: ManagedServiceV1, *, namespace: str) -> str:
    from runtime_hosting.kubernetes import KubernetesManagedServiceRenderer

    if service.port is None:
        raise ValueError("Managed MCP service is missing its port.")
    name = KubernetesManagedServiceRenderer.deployment_name(service.service_id)
    return f"http://{name}.{namespace}.svc.cluster.local:{service.port}{service.mcp_path}"


__all__ = ["ManagedMCPServicePublisher", "kubernetes_mcp_endpoint"]
