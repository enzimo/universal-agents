"""Version 1 secret-free messages exchanged with the secure MCP broker."""

from __future__ import annotations

from typing import Any, Literal, Optional, TypeAlias

from pydantic import ConfigDict, Field, JsonValue

from custom_messages.contracts.access_v2 import ScheduleRunIdentityV1, SignedTaskContextV1
from custom_messages.contracts.common_v1 import (
    ContractModel,
    PrincipalDescriptorV1,
)


MCP_CONTRACT_SCHEMA_VERSION: Literal[1] = 1
MCP_TOOL_USE_ID_MAX_LENGTH = 240
MCPJsonValue: TypeAlias = JsonValue


class MCPBrokerMessageV1(ContractModel):
    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        str_strip_whitespace=True,
        populate_by_name=True,
    )


class MCPToolDefinitionV1(MCPBrokerMessageV1):
    name: str = Field(min_length=1, max_length=128)
    description: str = Field(default="", max_length=4000)
    input_schema: dict[str, MCPJsonValue] = Field(alias="inputSchema")
    output_schema: Optional[dict[str, MCPJsonValue]] = Field(
        default=None,
        alias="outputSchema",
    )
    operation_digest: str = Field(pattern=r"^[0-9a-f]{64}$")


class MCPDiscoveryRequestV1(MCPBrokerMessageV1):
    """List an active connection's tools for an agent type.

    The broker checks that the connection is active and that the calling
    process (its token) may act for that agent type; listing tools needs no
    person. Whether an agent may call a tool is decided by its grants.
    """

    schema_version: Literal[1] = MCP_CONTRACT_SCHEMA_VERSION
    record_type: Literal["mcp_discovery_request"] = "mcp_discovery_request"
    record_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    named_agent_type: str = Field(min_length=1, max_length=240)


class MCPDiscoveryResponseV1(MCPBrokerMessageV1):
    schema_version: Literal[1] = MCP_CONTRACT_SCHEMA_VERSION
    record_type: Literal["mcp_discovery_response"] = "mcp_discovery_response"
    connection_id: str = Field(min_length=1, max_length=240)
    record_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    descriptor_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    operation_catalog_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    tools: tuple[MCPToolDefinitionV1, ...]


class MCPInvocationRequestV1(MCPBrokerMessageV1):
    schema_version: Literal[1] = MCP_CONTRACT_SCHEMA_VERSION
    record_type: Literal["mcp_invocation_request"] = "mcp_invocation_request"
    record_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    operation_name: str = Field(min_length=1, max_length=128)
    arguments: dict[str, MCPJsonValue] = Field(default_factory=dict)
    tool_use_id: str = Field(min_length=1, max_length=MCP_TOOL_USE_ID_MAX_LENGTH)
    task_id: str = Field(default="", max_length=240)
    principal: PrincipalDescriptorV1
    # Who the task works for, signed by the gateway or scheduler, or a
    # scheduled run's permit-backed identity; the broker trusts the person only
    # from one of these (access_control.task_identity).
    task_context: Optional[SignedTaskContextV1] = None
    schedule_run: Optional[ScheduleRunIdentityV1] = None


class MCPToolResultV1(MCPBrokerMessageV1):
    """MCP-native result; Strands projection remains in the agent adapter."""

    content: tuple[dict[str, MCPJsonValue], ...]
    structured_content: Optional[dict[str, MCPJsonValue]] = Field(
        default=None,
        alias="structuredContent",
    )
    meta: Optional[dict[str, MCPJsonValue]] = Field(default=None, alias="_meta")
    is_error: Optional[bool] = Field(default=None, alias="isError")

    @classmethod
    def from_sdk_result(cls, result: Any) -> "MCPToolResultV1":
        """The fields this contract carries, from an MCP SDK result of either line.

        Fields a newer SDK adds to the wire form (2.x's ``resultType``) are
        left out rather than refused, so the contract does not depend on the
        SDK version (access plan 7e).
        """

        wire = result.model_dump(mode="json", by_alias=True, exclude_none=True)
        carried = {field.alias or name for name, field in cls.model_fields.items()}
        return cls.model_validate({key: value for key, value in wire.items() if key in carried})


class MCPInvocationResponseV1(MCPBrokerMessageV1):
    schema_version: Literal[1] = MCP_CONTRACT_SCHEMA_VERSION
    record_type: Literal["mcp_invocation_response"] = "mcp_invocation_response"
    result: MCPToolResultV1


class MCPHostLifecycleRequestV1(MCPBrokerMessageV1):
    """Start or stop an installed hosted server (manager and gateway only).

    Whoever asked was authorized before this request: the per-call access
    hook for an agent, the access service for a person in the gateway.
    """

    schema_version: Literal[1] = MCP_CONTRACT_SCHEMA_VERSION
    record_type: Literal["mcp_host_lifecycle_request"] = "mcp_host_lifecycle_request"
    definition_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
