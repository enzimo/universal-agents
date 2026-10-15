"""Broker-owned MCP execution runtime and final local enforcement point.

- **Discovery** lists an active connection's tools for any agent type the
  calling process may act for; listing grants nothing.
- **Invocation** runs one operation of the connection's pinned catalog after
  the access gate (``secure_tool_broker.access_gate.check_mcp``) decided it:
  grants on ``mcp:<name>`` for the agent side and the person the signed task
  context names (access plan 7d). There is no rule that the person must be
  the one who proposed the connection: an installed server is a team tool.
- **Credentials** for a connection that declares credential slots come from
  account links, as for other tools: the calling person's own link first,
  then the team's (the gate resolves it). Discovery has no person, so it uses
  the link of the person the connection was proposed for, then the team's.
  One client (one authenticated MCP session) is kept per connection and link.
- **Refusals** of a call carry the gate's reason, tool, operation, level and
  next step (``MCPAccessRefused``), which the broker returns as the JSON body
  of its 403 so the agent's model can ask for exactly that access.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
from pathlib import Path
import re
from typing import Any, Mapping

from custom_messages.contracts.common_v1 import (
    PrincipalType,
)
from custom_messages.contracts.mcp_v1 import (
    MCPDiscoveryRequestV1,
    MCPDiscoveryResponseV1,
    MCPHostLifecycleRequestV1,
    MCPInvocationRequestV1,
    MCPToolDefinitionV1,
    MCPToolResultV1,
)
from env_secrets import (
    PROCESS_SECURE_BROKER,
    SECRET_ENV_LIST_PATH,
    get_env_with_file_fallback,
    is_withheld_from,
)
from mcp_control import sdk_compat
from mcp_control.operation_levels import normalize_hints, record_discovered_hints
from mcp_control.hosting import (
    MCPHostedServerStore,
    resolve_hosted_broker_config,
    resolve_mcp_hosting_paths,
)
from mcp_control.registry import (
    operation_permitted_by_descriptor,
    MCPConnectionLifecycle,
    MCPConnectionRecordV1,
    MCPConnectionSource,
    MCPConnectionStore,
    MCPConnectionTransport,
    MCPOperationDefinitionV1,
    resolve_mcp_connection_paths,
    resolve_record_source_config,
    validate_mcp_network_target,
)
from access_catalog.catalog import mcp_tool_id
from access_control.caller_tokens import BrokerCaller
from custom_messages.contracts.access_v2 import AccountLinkV1, InjectionPlacement
from secure_tool_broker.access_gate import REASON_ACCOUNT_LINK_MISSING, BrokerAccessGate
from secure_tool_broker.mcp_client import BrokerMCPClient
from secure_tool_broker.mcp_config import BrokerMCPConfig
from secure_tool_broker.mcp_hosting_runtime import MCPHostedProcessSupervisor
from secure_tool_broker.tool_setup_tracking import note_mcp_worked


LOGGER = logging.getLogger(__name__)
MCP_BROKER_COMPONENT = "secure_tool_broker.mcp"
_OPERATOR_ENV_REFERENCE = re.compile(
    r"\$(?:\{(?P<braced>[A-Z][A-Z0-9_]*)\}|(?P<plain>[A-Z][A-Z0-9_]*))"
)


def _operator_environment_reference_names(value: str) -> tuple[str, ...]:
    return tuple(
        sorted(
            {
                match.group("braced") or match.group("plain")
                for match in _OPERATOR_ENV_REFERENCE.finditer(value)
            }
        )
    )


def _operator_environment_values(value: str) -> dict[str, str]:
    """Values for the references in ``value``, each from NAME or its NAME_FILE secret file."""

    values: dict[str, str] = {}
    missing: list[str] = []
    for name in _operator_environment_reference_names(value):
        try:
            resolved = get_env_with_file_fallback(name)
        except (OSError, ValueError) as exc:
            LOGGER.warning(
                "An MCP environment reference points to an unreadable secret file. "
                "event=mcp_environment_reference_file_unreadable name=%s error_type=%s",
                name,
                type(exc).__name__,
            )
            raise PermissionError(
                f"The broker could not read the secret file named by {name}_FILE: {exc}"
            ) from exc
        if resolved:
            values[name] = resolved
        else:
            missing.append(name)
    if missing:
        raise _missing_references_error(missing)
    return values


def _missing_references_error(missing: list[str]) -> PermissionError:
    """Name the missing references, and say which ones the shared list keeps from the broker."""

    withheld = [name for name in missing if is_withheld_from(PROCESS_SECURE_BROKER, name)]
    hint = ""
    if withheld:
        LOGGER.warning(
            "An MCP environment reference names a secret the broker does not receive. "
            "event=mcp_environment_reference_withheld names=%s",
            ",".join(withheld),
        )
        hint = (
            f" {', '.join(withheld)} reach only the processes named in "
            f"{SECRET_ENV_LIST_PATH.name}; add secure_broker to that entry's receivers to use it here."
        )
    return PermissionError(
        "The broker process is missing required MCP environment "
        f"reference(s): {', '.join(missing)}.{hint}"
    )


def _materialize_operator_environment_string(value: str) -> str:
    values = _operator_environment_values(value)
    expanded = _OPERATOR_ENV_REFERENCE.sub(
        lambda match: values[match.group("braced") or match.group("plain")], value
    )
    if expanded.startswith("~"):
        return str(Path(expanded).expanduser())
    return expanded


def _materialize_operator_environment(value: Any) -> Any:
    """Resolve trusted config references inside the broker process.

    Callers must pass only an exact, digest-verified operator configuration and
    must run this before injecting broker-resolved credential values. This
    preserves the secret-free registry while ensuring credentials never need
    to be materialized in an agent process.
    """

    if isinstance(value, str):
        return _materialize_operator_environment_string(value)
    if isinstance(value, list):
        return [_materialize_operator_environment(item) for item in value]
    if isinstance(value, tuple):
        return tuple(_materialize_operator_environment(item) for item in value)
    if isinstance(value, Mapping):
        return {
            key: _materialize_operator_environment(item) for key, item in value.items()
        }
    return value


class MCPAccessRefused(PermissionError):
    """The access gate refused an MCP call; ``payload`` says why and what to do next."""

    def __init__(self, payload: dict[str, Any]) -> None:
        # The text is the payload itself, so logs show the reason code too.
        super().__init__(json.dumps(payload, sort_keys=True))
        self.payload = payload


class BrokerMCPRuntime:
    """Own concrete MCP sockets while exposing only neutral contracts."""

    def __init__(
        self,
        *,
        config: BrokerMCPConfig,
        access_gate: BrokerAccessGate,
    ) -> None:
        self.config = config
        self._access_gate = access_gate
        self.store = MCPConnectionStore(
            resolve_mcp_connection_paths(self.config.instance_root_path)
        )
        self._clients: dict[tuple[str, str], BrokerMCPClient] = {}
        self._client_authentication: dict[tuple[str, str], str] = {}
        self._locks: dict[str, asyncio.Lock] = {}
        self.hosting = MCPHostedProcessSupervisor(self.config)

    def _record(self, connection_id: str) -> MCPConnectionRecordV1:
        record = self.store.require(connection_id)
        if record.lifecycle != MCPConnectionLifecycle.ACTIVE:
            raise PermissionError("MCP connection is not active.")
        return record

    @staticmethod
    def _expanded_endpoint(raw: Mapping[str, Any]) -> str | None:
        endpoint = str(raw.get("server_url") or "").strip()
        env_name = str(raw.get("server_url_env") or "").strip()
        if not endpoint and env_name:
            endpoint = str(os.getenv(env_name) or "").strip()
        return os.path.expandvars(endpoint) if endpoint else None

    def _validate_transport_policy(
        self,
        record: MCPConnectionRecordV1,
        raw: Mapping[str, Any],
    ) -> tuple[str, ...]:
        if self.config.mcp.protocol_adapter != "mcp_v1":
            raise PermissionError("Unsupported configured MCP protocol adapter.")
        if (
            record.descriptor.transport == MCPConnectionTransport.LEGACY_SSE
            and not self.config.mcp.legacy_sse.enabled
        ):
            raise PermissionError("Legacy MCP SSE is disabled by operator policy.")
        if record.descriptor.transport == MCPConnectionTransport.STDIO:
            return ()
        endpoint = self._expanded_endpoint(raw)
        if not endpoint:
            raise PermissionError(
                "The broker could not resolve the approved MCP endpoint."
            )
        return validate_mcp_network_target(
            endpoint,
            allow_loopback=(
                True
                if record.descriptor.source.value == "hosted"
                else self.config.mcp.network_policy.allow_loopback
            ),
            allow_private_networks=(
                self.config.mcp.network_policy.allow_private_networks
            ),
            allowed_private_hosts=(
                self.config.mcp.network_policy.allowed_private_hosts
            ),
        )

    def _discovery_link(self, record: MCPConnectionRecordV1) -> AccountLinkV1 | None:
        """The account link discovery uses for a connection that declares credential slots.

        Listing tools happens without a person, so it uses the link of the
        person the connection was proposed for, then the team's
        (``BrokerAccessGate.account_link_for``). Calls use the caller's own
        link instead (``invoke``).
        """

        if not record.descriptor.credential_injections:
            return None
        link = self._access_gate.account_link_for(
            mcp_tool_id(record.descriptor.connection_name), record.descriptor.owner_canonical_user_id
        )
        if link is None:
            raise PermissionError(
                f"No account is linked for the MCP connection {record.descriptor.connection_name}. "
                "Ask TeamLead to request an account link for it."
            )
        return link

    @staticmethod
    def _inject_linked_credentials(link: AccountLinkV1, raw: dict[str, Any]) -> dict[str, Any]:
        from secure_tool_broker.credential_catalog import _resolve_injections

        resolved = _resolve_injections(link, frozenset({InjectionPlacement.HEADER, InjectionPlacement.ENV}))
        if not isinstance(resolved, tuple):
            raise PermissionError(
                f"The linked credential for this MCP connection could not be read: {resolved.error}"
            )
        values, _secrets = resolved
        prepared = dict(raw)
        prepared["headers"] = dict(prepared.get("headers") or {})
        prepared["env"] = dict(prepared.get("env") or {})
        for (placement, name), value in values.items():
            target = "headers" if placement is InjectionPlacement.HEADER else "env"
            prepared[target][name] = value
        return prepared

    async def _authenticated_config(
        self,
        record: MCPConnectionRecordV1,
        *,
        link: AccountLinkV1 | None,
    ) -> dict[str, Any]:
        """Resolve trusted config, then insert the linked account's credential."""
        if record.descriptor.source.value == "hosted":
            hosted_server_id = record.descriptor.source_locator.removeprefix("hosted:")
            await self.hosting.ensure_running(hosted_server_id)
            source_config = resolve_hosted_broker_config(
                self.config,
                hosted_server_id,
            )
        else:
            source_config = resolve_record_source_config(self.config, record)
        if link is not None:
            # The link owns these headers. Remove operator templates of the
            # same name before expansion so a missing env var cannot block it.
            linked_headers = {
                item.name.lower() for item in link.injections if item.placement is InjectionPlacement.HEADER
            }
            source_config = dict(source_config)
            source_config["headers"] = {
                name: value
                for name, value in dict(source_config.get("headers") or {}).items()
                if name.lower() not in linked_headers
            }
        if record.descriptor.source in {
            MCPConnectionSource.STATIC_GLOBAL,
            MCPConnectionSource.STATIC_AGENT_TYPE,
            MCPConnectionSource.CONFIGURED_PROFILE,
        }:
            # Static source configs are digest-verified immediately above.
            # Materialize their operator-owned references here, before
            # broker-resolved credentials are injected, so a credential
            # value containing '$' is never interpreted as a template.
            source_config = _materialize_operator_environment(source_config)
        raw = dict(source_config)
        return self._inject_linked_credentials(link, raw) if link is not None else raw

    async def _discard_client(self, cache_key: tuple[str, str]) -> None:
        existing = self._clients.pop(cache_key, None)
        self._client_authentication.pop(cache_key, None)
        if existing is not None:
            await existing.close()

    async def _client(self, record: MCPConnectionRecordV1, *, link: AccountLinkV1 | None) -> BrokerMCPClient:
        """The connection's client for one account link (one MCP session per link), made on first use."""

        cache_key = (record.connection_id, link.link_id if link is not None else "")
        lock = self._locks.setdefault(record.connection_id, asyncio.Lock())
        async with lock:
            existing = self._clients.get(cache_key)
            if (
                existing is not None
                and link is None
                and cache_key not in self._client_authentication
            ):
                return existing
            try:
                raw = await self._authenticated_config(record, link=link)
            except PermissionError:
                await self._discard_client(cache_key)
                raise
            fingerprint = hashlib.sha256(
                json.dumps(raw, sort_keys=True, default=str).encode()
            ).hexdigest()
            if existing is not None:
                if self._client_authentication.get(cache_key) == fingerprint:
                    return existing
                await self._discard_client(cache_key)
            pinned_addresses = self._validate_transport_policy(record, raw)
            if pinned_addresses:
                raw["mcp_pinned_addresses"] = list(pinned_addresses)
            client = BrokerMCPClient(raw)
            self._clients[cache_key] = client
            if link is not None:
                self._client_authentication[cache_key] = fingerprint
            return client

    async def change_host_lifecycle(
        self,
        hosted_server_id: str,
        payload: MCPHostLifecycleRequestV1,
        *,
        operation: str,
    ) -> dict[str, Any]:
        """Start or stop an installed server; the route admits only the manager and the gateway."""

        if operation not in {"start", "stop"}:
            raise ValueError("Unsupported hosted MCP broker operation.")
        definition = MCPHostedServerStore(resolve_mcp_hosting_paths(self.config)).require(hosted_server_id)
        if definition.definition_digest != payload.definition_digest:
            raise PermissionError("Hosted MCP definition changed before dispatch.")
        if operation == "start":
            return await self.hosting.start(hosted_server_id)
        for key, client in list(self._clients.items()):
            record = self.store.get(key[0])
            if (
                record is not None
                and record.descriptor.source_locator == f"hosted:{hosted_server_id}"
            ):
                await client.close()
                self._clients.pop(key, None)
        return await self.hosting.stop(hosted_server_id)

    async def restore_hosted(self) -> None:
        await self.hosting.restore()

    async def close(self) -> None:
        for client in list(self._clients.values()):
            try:
                await client.close()
            except Exception:
                LOGGER.warning("MCP client cleanup failed.", exc_info=True)
        self._clients.clear()
        self._client_authentication.clear()
        await self.hosting.close()

    async def discover_tools(
        self,
        connection_id: str,
        payload: MCPDiscoveryRequestV1,
        *,
        caller: BrokerCaller,
    ) -> MCPDiscoveryResponseV1:
        """The tools of an active connection, for an agent type the calling process may act for.

        Listing tools grants nothing: each call is decided by the access gate
        (``invoke``), so an agent sees a server's tools only once it holds
        access to it (``custom_agents.tool_sync``). The first listing pins the
        operation catalog; later listings return the pinned catalog.
        """

        record = self._record(connection_id)
        if record.record_digest != payload.record_digest:
            raise PermissionError("MCP connection record changed before discovery.")
        agent_type = payload.named_agent_type
        if not caller.runs_agents or caller.agent_type not in (None, agent_type):
            raise PermissionError(f"The {caller.label} process cannot list tools for {agent_type}.")
        if record.operations:
            return self._discovery_payload(record)
        try:
            link = self._discovery_link(record)
        except PermissionError:
            await self._discard_connection_clients(record.connection_id)
            raise
        client = await self._client(record, link=link)
        tools = await client.list_tools()
        permitted = [
            tool
            for tool in tools
            if operation_permitted_by_descriptor(record.descriptor, str(tool.name))
        ]
        if len(permitted) != len(tools):
            LOGGER.info(
                "MCP discovery omitted operations excluded by the connection filters. "
                "event=mcp_operation_catalog_filtered connection_id=%s advertised=%s kept=%s",
                record.connection_id,
                len(tools),
                len(permitted),
            )
        operations = tuple(
            MCPOperationDefinitionV1.build(
                operation_name=str(tool.name),
                description=str(tool.description or ""),
                input_schema=sdk_compat.input_schema(tool),
                output_schema=sdk_compat.output_schema(tool),
            )
            for tool in permitted
        )
        record = self.store.set_operations(record.connection_id, operations)
        # Annotations are untrusted hints; they only suggest a level that an
        # operator confirms (mcp_control.operation_levels).
        record_discovered_hints(
            self.store.paths,
            record.connection_id,
            record.operations,
            {str(tool.name): normalize_hints(getattr(tool, "annotations", None)) for tool in permitted},
        )
        LOGGER.info(
            "MCP operation catalog attested. event=mcp_operation_catalog_attested "
            "connection_id=%s operation_count=%s catalog_sha256=%s",
            record.connection_id,
            len(record.operations),
            record.operation_catalog_sha256,
        )
        return self._discovery_payload(record)

    @staticmethod
    def _discovery_payload(record: MCPConnectionRecordV1) -> MCPDiscoveryResponseV1:
        return MCPDiscoveryResponseV1(
            connection_id=record.connection_id,
            record_digest=record.record_digest,
            descriptor_sha256=record.descriptor.descriptor_sha256,
            operation_catalog_sha256=record.operation_catalog_sha256,
            tools=tuple(
                MCPToolDefinitionV1(
                    name=item.operation_name,
                    description=item.description,
                    input_schema=item.input_schema,
                    output_schema=item.output_schema,
                    operation_digest=item.operation_digest,
                )
                for item in record.operations
            ),
        )

    @staticmethod
    def _installed_operation(record: MCPConnectionRecordV1, payload: MCPInvocationRequestV1) -> Any:
        if record.record_digest != payload.record_digest:
            raise PermissionError("MCP connection record changed before invocation.")
        operation = next(
            (item for item in record.operations if item.operation_name == payload.operation_name),
            None,
        )
        if operation is None:
            raise PermissionError("MCP operation is absent from the installed catalog.")
        if not operation_permitted_by_descriptor(record.descriptor, operation.operation_name):
            raise PermissionError("MCP operation is excluded by the connection's include_tools/exclude_tools.")
        return operation

    async def _discard_connection_clients(self, connection_id: str) -> None:
        for cache_key in [key for key in self._clients if key[0] == connection_id]:
            await self._discard_client(cache_key)

    async def _require_access(
        self, caller: BrokerCaller, record: MCPConnectionRecordV1, operation_name: str, payload: MCPInvocationRequestV1
    ) -> AccountLinkV1 | None:
        """Decide the call (grants on both sides) and return the account link it uses, if any."""

        principal = payload.principal
        if principal.principal_type != PrincipalType.AGENT:
            raise PermissionError("Brokered MCP invocation requires an agent principal.")
        gate = self._access_gate.check_mcp(
            caller,
            record.descriptor.connection_name,
            operation_name,
            {
                "task_context": payload.task_context,
                "schedule_run": payload.schedule_run,
                "canonical_user_id": principal.on_behalf_of_user_id,
                "agent_id": principal.principal_id,
                "named_agent_type": principal.named_agent_type,
                "team_id": principal.team_id,
                "task_id": payload.task_id,
            },
        )
        if gate.allowed:
            return gate.account_link
        if gate.reason_code == REASON_ACCOUNT_LINK_MISSING:
            # The account was unlinked: close connections that still hold it.
            await self._discard_connection_clients(record.connection_id)
        raise MCPAccessRefused(gate.as_error())

    async def invoke(
        self,
        connection_id: str,
        payload: MCPInvocationRequestV1,
        *,
        caller: BrokerCaller,
    ) -> MCPToolResultV1:
        record = self._record(connection_id)
        operation = self._installed_operation(record, payload)
        link = await self._require_access(caller, record, operation.operation_name, payload)
        client = await self._client(record, link=link)
        result = await client.call_tool(operation.operation_name, payload.arguments)
        note_mcp_worked(
            self._access_gate,
            mcp_tool_id(record.descriptor.connection_name),
            payload.principal.on_behalf_of_user_id,
            link,
        )
        LOGGER.info(
            "Brokered MCP operation completed. event=mcp_broker_invocation_completed "
            "connection_id=%s operation=%s agent_id=%s task_id=%s",
            record.connection_id,
            operation.operation_name,
            payload.principal.principal_id,
            payload.task_id,
        )
        return MCPToolResultV1.from_sdk_result(result)


__all__ = ["BrokerMCPRuntime", "MCPAccessRefused", "MCP_BROKER_COMPONENT"]
