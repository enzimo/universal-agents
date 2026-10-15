"""
Strands MCP clients for Universal Agents.

- ``BrokeredMCPClient``: the agent side of an installed MCP connection whose
  real connection lives in the secure broker. It lists the server's tools and
  forwards each call with the agent and its signed task context; the broker
  decides the call. A refused call returns the broker's reason, tool,
  operation, level and next step to the model (``BrokerRefusal``), so the
  agent can ask for exactly the access it lacks.
- ``brokered_mcp_client`` builds the client of one installed server from its
  secret-free proxy configuration; ``custom_agents.mcp_server_tools`` loads
  its tools before model calls in both authorization modes and decides when a
  failed listing is tried again. Agent processes never open an MCP transport
  themselves, so there is no agent-side reconnect or session handling: the
  broker owns each session (``secure_tool_broker.mcp_client``).
- ``resolve_mcp_profile_configs`` and ``merge_external_mcp_server_configs``
  turn the operator's launch profiles (``mcp_profile_names``) into configured
  servers, which ``custom_agents.providers.mcp_connections`` reduces to the
  ids of their installed records.
"""

from __future__ import annotations

import hashlib
import json
import logging
import math
import os
import threading
from collections.abc import Mapping
from datetime import timedelta
from typing import Any

import httpx
from mcp import types
from strands.tools.mcp import MCPClient
from strands.tools.mcp.mcp_agent_tool import MCPAgentTool
from strands.tools.mcp.mcp_types import MCPToolResult

from custom_messages.contracts.common_v1 import PrincipalDescriptorV1, PrincipalType
from custom_messages.contracts.mcp_v1 import (
    MCPDiscoveryRequestV1,
    MCPDiscoveryResponseV1,
    MCPInvocationRequestV1,
    MCPInvocationResponseV1,
    MCP_TOOL_USE_ID_MAX_LENGTH,
)
from custom_tools.runtime_context import active_schedule_run, active_signed_task_context, get_agent_ref


LOGGER = logging.getLogger(__name__)
DEFAULT_MCP_TOOL_TIMEOUT_SECONDS = 300.0


def _broker_tool_use_id(tool_use_id: str) -> str:
    """Return a bounded broker correlation id without changing Strands identity."""

    normalized = str(tool_use_id or "").strip()
    if not normalized:
        raise ValueError("Brokered MCP invocation requires a tool_use_id.")
    if len(normalized) <= MCP_TOOL_USE_ID_MAX_LENGTH:
        return normalized
    digest = hashlib.sha256(normalized.encode("utf-8")).hexdigest()
    return f"mcp-tool-use:sha256:{digest}"


class BrokerRefusal(PermissionError):
    """The broker refused an MCP request; ``payload`` is its JSON body.

    For an access refusal the body names the reason code, the catalog tool,
    operation and level, the reason and the next step
    (``secure_tool_broker.access_gate.GateResult.as_error``).
    """

    def __init__(self, status_code: int, payload: Mapping[str, Any]) -> None:
        self.status_code = status_code
        self.payload = dict(payload)
        super().__init__(str(self.payload.get("error") or f"The secure broker refused the request (HTTP {status_code})."))

    def model_text(self) -> str:
        """What the model reads: the refusal in the shape of the runtime's own access refusals.

        With the tool, level and next step the agent can ask for exactly the
        access it lacks (``request_access``), or for an account link.
        """

        payload = self.payload
        facts = {
            "status": "refused",
            "reason_code": payload.get("reason_code") or "refused",
            "reason": payload.get("error") or str(self),
            "tool_id": payload.get("tool_id"),
            "operation": payload.get("operation"),
            "level": payload.get("level"),
            "next_step": payload.get("next_step"),
        }
        return json.dumps({key: value for key, value in facts.items() if value}, sort_keys=True)


def _broker_refusal(response: httpx.Response) -> BrokerRefusal:
    try:
        body = response.json()
    except ValueError:
        body = None
    if not isinstance(body, dict):
        body = {"error": response.text.strip()[:500] or f"HTTP {response.status_code}"}
    return BrokerRefusal(response.status_code, body)


def secure_broker_connection() -> tuple[str, str]:
    """Resolve the internal broker URL and bearer token for trusted clients."""

    base_url = (
        str(os.getenv("SECURE_TOOL_BROKER_INTERNAL_BASE_URL") or "").strip().rstrip("/")
    )
    token = str(os.getenv("SECURE_TOOL_BROKER_TOKEN") or "").strip()
    if not base_url or not token:
        raise PermissionError(
            "Brokered MCP requires the configured secure tool broker."
        )
    return base_url, token


def _tool_timeout_seconds(server_config: Mapping[str, Any]) -> float:
    raw_value = server_config.get(
        "tool_timeout_seconds", DEFAULT_MCP_TOOL_TIMEOUT_SECONDS
    )
    try:
        timeout_seconds = float(raw_value)
    except (TypeError, ValueError) as exc:
        raise ValueError(
            "MCP `tool_timeout_seconds` must be a positive number."
        ) from exc
    if not math.isfinite(timeout_seconds) or timeout_seconds <= 0:
        raise ValueError("MCP `tool_timeout_seconds` must be a positive number.")
    return timeout_seconds


class _UnreachableBrokeredTransport:
    """Fail closed if Strands tries to open a broker-owned MCP transport locally."""

    async def __aenter__(self) -> None:
        raise RuntimeError(
            "Brokered MCP clients do not open transports in agent processes."
        )

    async def __aexit__(
        self,
        _exc_type: Any,
        _exc_value: Any,
        _traceback: Any,
    ) -> None:
        return None


def _unreachable_brokered_transport() -> _UnreachableBrokeredTransport:
    return _UnreachableBrokeredTransport()


class BrokeredMCPClient(MCPClient):
    """Schema/result proxy whose concrete MCP connection lives in the broker.

    It subclasses Strands' ``MCPClient`` only so Strands' ``MCPAgentTool`` can
    call it: the tools it lists call ``call_tool_async``, which forwards to
    the broker. It never opens a session, so Strands' connection lifecycle
    (start, stop, reconnects, synchronous calls) does not apply to it.
    """

    def __init__(
        self,
        *,
        connection_id: str,
        record_digest: str,
        tool_name_prefix: str,
        tool_timeout_seconds: float,
        named_agent_type: str,
    ) -> None:
        super().__init__(_unreachable_brokered_transport, prefix=tool_name_prefix)
        self._ua_connection_id = str(connection_id or "").strip()
        self._ua_record_digest = str(record_digest or "").strip()
        self._ua_tool_name_prefix = tool_name_prefix
        self._ua_tool_timeout = timedelta(seconds=tool_timeout_seconds)
        self._ua_named_agent_type = str(named_agent_type or "").strip()

    @property
    def ua_connection_id(self) -> str:
        return self._ua_connection_id

    @property
    def ua_record_digest(self) -> str:
        return self._ua_record_digest

    async def _post(self, suffix: str, payload: Mapping[str, Any]) -> dict[str, Any]:
        base_url, token = secure_broker_connection()
        timeout = max(5.0, self._ua_tool_timeout.total_seconds())
        async with httpx.AsyncClient(timeout=timeout) as client:
            response = await client.post(
                f"{base_url}/v1/mcp/connections/{self._ua_connection_id}/{suffix}",
                headers={"Authorization": f"Bearer {token}"},
                json=dict(payload),
            )
        if response.status_code == httpx.codes.FORBIDDEN:
            raise _broker_refusal(response)
        response.raise_for_status()
        body = response.json()
        if not isinstance(body, dict):
            raise RuntimeError("Secure broker returned an invalid MCP response.")
        return body

    async def load_tools(self, **_: Any) -> list[MCPAgentTool]:
        """List the server's tools through the broker; a failure raises.

        The loader (``custom_agents.mcp_server_tools``) decides what happens
        next: a refusal (``BrokerRefusal``) waits for access or the server to
        change, anything else is tried again on a schedule.
        """

        if self._loaded_tools is not None:
            return self._loaded_tools
        # The broker lists an active connection's tools for this agent type
        # (the caller's process token identifies the process); listing grants
        # nothing, every call is decided by the broker.
        request = MCPDiscoveryRequestV1(
            record_digest=self._ua_record_digest,
            named_agent_type=self._ua_named_agent_type,
        )
        raw_body = await self._post(
            "tools",
            request.model_dump(mode="json", by_alias=True, exclude_none=True),
        )
        body = MCPDiscoveryResponseV1.model_validate(raw_body)
        self._ua_record_digest = body.record_digest
        self._loaded_tools = [
            MCPAgentTool(
                types.Tool.model_validate(
                    {
                        "name": item.name,
                        "description": item.description,
                        "inputSchema": item.input_schema,
                        "outputSchema": item.output_schema,
                    }
                ),
                self,
                name_override=f"{self._ua_tool_name_prefix}_{item.name}",
                timeout=self._ua_tool_timeout,
            )
            for item in body.tools
        ]
        return self._loaded_tools

    async def call_tool_async(
        self,
        tool_use_id: str,
        name: str,
        arguments: dict[str, Any] | None = None,
        read_timeout_seconds: timedelta | None = None,
        meta: dict[str, Any] | None = None,
        progress_callback: Any = None,
        *,
        cancel_signal: threading.Event | None = None,
    ):
        del read_timeout_seconds, meta, progress_callback, cancel_signal
        try:
            from custom_agents.access_runtime import acting_agent

            agent = get_agent_ref()
            if agent is None:
                raise PermissionError(
                    "Brokered MCP invocation has no runtime agent context."
                )
            # The broker checks the person against the signed task context, and
            # task-bound grants against the task the runtime is running now
            # (the same task the signed context was read from).
            actor = acting_agent(agent)
            task_id = str(actor.task_id or "").strip()
            request = MCPInvocationRequestV1(
                record_digest=self._ua_record_digest,
                operation_name=name,
                arguments=dict(arguments or {}),
                # Gemini thinking models may embed an opaque thought signature in
                # this id. Strands needs the original value when the result returns,
                # but the broker uses only a bounded correlation identifier.
                tool_use_id=_broker_tool_use_id(tool_use_id),
                task_id=task_id,
                principal=PrincipalDescriptorV1(
                    principal_type=PrincipalType.AGENT,
                    principal_id=actor.agent.agent_id,
                    on_behalf_of_user_id=actor.person_id,
                    team_id=actor.agent.team_id,
                    roles=(actor.agent.role,),
                    named_agent_type=actor.agent.agent_type,
                ),
                task_context=active_signed_task_context(agent),
                schedule_run=active_schedule_run(agent),
            )
            raw_body = await self._post(
                "invoke",
                request.model_dump(mode="json", by_alias=True, exclude_none=True),
            )
            response = MCPInvocationResponseV1.model_validate(raw_body)
            result = types.CallToolResult.model_validate(
                response.result.model_dump(
                    mode="json",
                    by_alias=True,
                    exclude_none=True,
                )
            )
            return self._handle_tool_result(tool_use_id, result)
        except BrokerRefusal as refusal:
            LOGGER.warning(
                "The broker refused an MCP call; the model is told why. event=mcp_broker_invocation_refused "
                "connection_id=%s operation=%s reason_code=%s",
                self._ua_connection_id,
                name,
                refusal.payload.get("reason_code"),
            )
            return MCPToolResult(
                status="error",
                toolUseId=tool_use_id,
                content=[{"text": refusal.model_text()}],
            )
        except Exception as exc:
            LOGGER.exception(
                "Brokered MCP invocation failed. event=mcp_broker_invocation_client_failed "
                "connection_id=%s operation=%s",
                self._ua_connection_id,
                name,
            )
            return self._handle_tool_execution_error(tool_use_id, exc)



def brokered_mcp_client(server_config: Mapping[str, Any]) -> BrokeredMCPClient:
    """The agent-side client of one installed server.

    ``server_config`` is the secret-free proxy configuration of an installed
    record (``mcp_control.registry.brokered_mcp_server_config``): it names the
    connection and record digest, never an endpoint or credential, because the
    connection itself lives in the broker.
    """

    name = str(server_config.get("name") or "").strip()
    connection_id = str(server_config.get("mcp_connection_id") or "").strip()
    record_digest = str(server_config.get("mcp_connection_record_digest") or "").strip()
    if not server_config.get("mcp_brokered") or not connection_id or not record_digest or not name:
        raise ValueError("Brokered MCP configuration is missing its installed identity.")
    client = BrokeredMCPClient(
        connection_id=connection_id,
        record_digest=record_digest,
        tool_name_prefix=str(server_config.get("tool_name_prefix") or name).strip(),
        tool_timeout_seconds=_tool_timeout_seconds(server_config),
        named_agent_type=str(server_config.get("mcp_target_agent_type") or ""),
    )
    LOGGER.info(
        "Configured broker-owned MCP connection. event=mcp_broker_client_configured connection_id=%s profile_name=%s",
        connection_id,
        name,
    )
    return client


def strip_mcp_attachment_metadata(profile_config: Mapping[str, Any]) -> dict[str, Any]:
    """Remove control-plane-only ``attachment`` metadata before MCP construction."""

    return {
        key: value for key, value in dict(profile_config).items() if key != "attachment"
    }


def resolve_mcp_profile_configs(
    profile_names: list[str] | None,
    available_profiles: Mapping[str, Any] | None,
) -> list[dict[str, Any]]:
    """Resolve trusted MCP profile names into concrete server configs."""
    if not profile_names:
        return []

    profiles = dict(available_profiles or {})
    resolved: list[dict[str, Any]] = []
    missing = [name for name in profile_names if name not in profiles]
    if missing:
        raise ValueError(f"Unknown MCP profile(s): {', '.join(missing)}")

    for profile_name in profile_names:
        raw_profile = profiles[profile_name]
        if not isinstance(raw_profile, Mapping):
            raise ValueError(f"MCP profile '{profile_name}' must be a mapping.")
        profile_config = strip_mcp_attachment_metadata(raw_profile)
        profile_config.setdefault("name", profile_name)
        resolved.append(profile_config)

    return resolved


def merge_external_mcp_server_configs(
    base_configs: list[dict[str, Any]] | None,
    additional_configs: list[dict[str, Any]] | None,
) -> list[dict[str, Any]]:
    """Merge startup MCP server configs, overwriting by named entry when repeated."""
    merged: list[dict[str, Any]] = []
    index_by_name: dict[str, int] = {}

    for raw_config in list(base_configs or []) + list(additional_configs or []):
        config = dict(raw_config)
        config_name = config.get("name")
        if config_name and config_name in index_by_name:
            merged[index_by_name[config_name]] = config
            continue
        if config_name:
            index_by_name[config_name] = len(merged)
        merged.append(config)

    return merged
