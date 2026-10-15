"""Installed MCP connections: startup reconciliation, proposals and lifecycle changes.

- ``apply_mcp_connection_reconciliation`` records the operator's configured
  servers in the MCP registry and replaces them, in an agent's runtime
  configuration, with the ids of their records (``CONFIGURED_CONNECTION_IDS``);
  an agent never receives a server's configuration, and nothing connects
  before approval. Agents load servers before model calls
  (``custom_agents.mcp_server_tools``): with authorization on from their
  grants (``custom_agents.tool_sync``, access plan 7d), with it off the
  active servers ``shown_without_authorization`` picks.
- Agents propose inert connections (``propose_by_agent``); installing one is
  an item of an access proposal an administrator approves
  (``access_control.proposals``), or an administrator approves it in the
  gateway's MCP controls.
- The gateway's MCP controls use the ``*_by_person`` methods after checking
  the person through the access service.
"""

from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Any, Collection, Mapping, Sequence

from custom_agents.access_runtime import acting_agent
from custom_agents.runtime_paths import resolve_runtime_instance_root
from custom_messages.contracts.access_v2 import ChangeApprovalMode, ChangeApprovalRecordV1
from custom_messages.contracts.common_v1 import utc_now
from id_generation import new_id_suffix
from mcp_control.registry import (
    MCPConnectionLifecycle,
    MCPConnectionPaths,
    MCPConnectionRecordV1,
    MCPConnectionSource,
    MCPConnectionStore,
    MCPConnectionTransport,
    _new_record,
    _record_for_config,
    descriptor_from_server_config,
    normalize_mcp_transport,
    reconcile_instance_mcp_connections,
    resolve_mcp_connection_paths,
)

# Records that may still become active or are active; a new proposal with the
# same name is refused unless it is the same configuration.
_LIVE_LIFECYCLES = frozenset({MCPConnectionLifecycle.PENDING, MCPConnectionLifecycle.ACTIVE})

LOGGER = logging.getLogger(__name__)


_CONFIGURED_SOURCES = (
    MCPConnectionSource.STATIC_GLOBAL,
    MCPConnectionSource.STATIC_AGENT_TYPE,
    MCPConnectionSource.CONFIGURED_PROFILE,
)
_INSTALLED_SOURCES = frozenset({MCPConnectionSource.AGENT_PROPOSAL, MCPConnectionSource.HOSTED})

# The agent runtime configuration key (and ``UniversalAgent`` field) with the
# records of the configured servers an agent's own configuration lists.
CONFIGURED_CONNECTION_IDS = "mcp_configured_connection_ids"


def _withheld_without_registry(agent_config: dict[str, Any]) -> dict[str, Any]:
    """No instance registry: configured servers cannot be checked, so none are kept."""

    # Launch profiles were already merged into ``external_mcp_servers``.
    configured_servers = list(agent_config.pop("external_mcp_servers", None) or [])
    agent_config[CONFIGURED_CONNECTION_IDS] = []
    if configured_servers:
        LOGGER.warning(
            "MCP configuration was withheld because no installed-registry "
            "instance boundary is available. "
            "event=mcp_connection_registry_instance_missing agent_type=%s server_count=%s",
            str(agent_config.get("named_agent_type") or "").strip(),
            len(configured_servers),
        )
    return agent_config


def _configured_records(
    store: MCPConnectionStore, raw_servers: Sequence[Any], agent_type: str
) -> tuple[list[MCPConnectionRecordV1], list[str]]:
    """The records of the configured servers this agent's configuration lists for its type, and the names not active.

    Records still waiting for review are kept: once an administrator approves
    one, the agent loads it without a restart.
    """

    records: list[MCPConnectionRecordV1] = []
    waiting: list[str] = []
    for raw in raw_servers:
        if not isinstance(raw, Mapping) or raw.get("enabled", True) is False:
            continue
        record = _record_for_config(store, raw, source_candidates=_CONFIGURED_SOURCES)
        if record is None or not record.permits_agent_type(agent_type):
            waiting.append(record.descriptor.connection_name if record is not None else str(raw.get("name") or "unnamed"))
            continue
        records.append(record)
        if record.lifecycle != MCPConnectionLifecycle.ACTIVE:
            waiting.append(record.descriptor.connection_name)
    return records, waiting


def shown_without_authorization(record: MCPConnectionRecordV1, *, agent_type: str, configured_ids: Collection[str]) -> bool:
    """With authorization off (decision D8): whether an agent of this type shows this server.

    Installed servers (TeamLead's proposals and hosted servers) follow the
    agent types their record names. A server from the operator's
    configuration follows that configuration (``configured_ids``): the global
    list or the agent type's own list, plus the profiles the agent was
    launched with. The caller checks that the record is active.
    """

    if record.descriptor.source in _INSTALLED_SOURCES:
        return record.permits_agent_type(agent_type)
    return record.connection_id in configured_ids


def apply_mcp_connection_reconciliation(agent_config: dict[str, Any]) -> dict[str, Any]:
    """Filter desired static MCP config through the installed registry.

    Replaces the agent's configured servers with the ids of their records
    (``CONFIGURED_CONNECTION_IDS``); the agent loads the active ones while it
    runs (``custom_agents.mcp_server_tools``). New or changed configured
    servers wait for review and are logged.
    """

    instance_config_path = str(agent_config.get("instance_config_path") or "").strip()
    if not instance_config_path or not Path(instance_config_path).is_file():
        return _withheld_without_registry(agent_config)
    from assistant_gateway.config import GatewayInstanceConfig

    config = GatewayInstanceConfig.load_for_process(instance_config_path)
    reconcile_instance_mcp_connections(config)
    store = MCPConnectionStore(resolve_mcp_connection_paths(config.instance_root_path))
    agent_type = str(agent_config.get("named_agent_type") or "").strip()
    configured, waiting = _configured_records(store, list(agent_config.pop("external_mcp_servers", None) or []), agent_type)
    agent_config[CONFIGURED_CONNECTION_IDS] = [record.connection_id for record in configured]
    if waiting:
        LOGGER.warning(
            "New or changed MCP definitions are quarantined before network access. "
            "event=mcp_static_connections_quarantined agent_type=%s connection_names=%s",
            agent_type,
            sorted(set(waiting)),
        )
    return agent_config


class MCPConnectionAdministration:
    """Typed lifecycle changes to installed MCP connections.

    Agents propose connections here; installing one is an item of an access
    proposal (``access_control.proposals``). The ``*_by_person`` methods are
    the gateway's MCP controls: the gateway has already checked through the
    access service that the signed-in person may make the change, and the
    approval records that person.
    """

    def __init__(self, store: MCPConnectionStore) -> None:
        self.store = store

    @staticmethod
    def is_pending(record: MCPConnectionRecordV1) -> bool:
        return bool(record.lifecycle == MCPConnectionLifecycle.PENDING)

    @classmethod
    def for_instance_config(cls, config: Any) -> "MCPConnectionAdministration":
        return cls(
            MCPConnectionStore(resolve_mcp_connection_paths(config.instance_root_path))
        )

    def list_public(self) -> list[dict[str, Any]]:
        return [
            {
                "connection_id": record.connection_id,
                "record_digest": record.record_digest,
                "lifecycle": record.lifecycle.value,
                "connection_name": record.descriptor.connection_name,
                "transport": record.descriptor.transport.value,
                "source": record.descriptor.source.value,
                "source_locator": record.descriptor.source_locator,
                "endpoint": record.descriptor.endpoint,
                "server_url_env": record.descriptor.server_url_env,
                "tool_name_prefix": record.descriptor.tool_name_prefix,
                "target_agent_types": list(record.target_agent_types),
                # Grants on this catalog tool decide who may use the server.
                "tool_id": record.descriptor.capability_id,
                "operation_count": len(record.operations),
                "operations": [item.operation_name for item in record.operations],
                # The broker fills declared credential slots from the account
                # linked to the connection's catalog tool (mcp:<name>).
                "credential_mode": (
                    "linked_account"
                    if record.descriptor.credential_injections
                    else (
                        "operator_configured"
                        if record.descriptor.requires_legacy_credentials
                        else "none"
                    )
                ),
                "legacy_sse": record.descriptor.transport
                == MCPConnectionTransport.LEGACY_SSE,
                "from_operator_configuration": record.from_operator_configuration,
                "approval": (
                    record.approval.model_dump(mode="json") if record.approval is not None else None
                ),
                "created_at_utc": record.created_at_utc.isoformat(),
                "updated_at_utc": record.updated_at_utc.isoformat(),
                "rejection_reason": record.rejection_reason,
                "drift_reason": record.drift_reason,
            }
            for record in self.store.list_records()
        ]

    def propose_by_agent(
        self,
        agent: Any,
        *,
        connection_name: str,
        endpoint: str,
        transport: str = "streamable_http",
        tool_name_prefix: str = "",
        include_tools: Sequence[str] = (),
        exclude_tools: Sequence[str] = (),
        target_agent_types: Sequence[str] = (),
        credential_injections: Sequence[Mapping[str, Any]] = (),
        allow_legacy_sse: bool = False,
    ) -> MCPConnectionRecordV1:
        """Persist an inert, secret-free remote connection proposal.

        Proposing the same configuration again returns the pending (or already
        installed) record, so a tool call that is repeated or resumed does not
        leave a second record behind.
        """

        actor = acting_agent(agent)
        if not actor.person_id:
            raise PermissionError("Propose MCP connections from a task that works for a person.")
        normalized_transport = self._proposable_transport(transport, allow_legacy_sse=allow_legacy_sse)
        normalized_targets = self._target_types(target_agent_types or (actor.agent.agent_type,))
        proposal_nonce = new_id_suffix()
        raw = {
            "name": connection_name,
            "transport": normalized_transport.value,
            "server_url": endpoint,
            "tool_name_prefix": tool_name_prefix or connection_name,
            "include_tools": list(include_tools),
            "exclude_tools": list(exclude_tools),
            "credential_injections": [dict(item) for item in credential_injections],
        }
        descriptor = descriptor_from_server_config(
            raw,
            source=MCPConnectionSource.AGENT_PROPOSAL,
            source_locator=f"agent-proposal:{actor.agent.agent_id}:{proposal_nonce}",
            team_id=actor.agent.team_id,
            owner_canonical_user_id=actor.person_id,
        )
        existing = self._live_record_named(descriptor.connection_name)
        if existing is not None:
            if self._same_proposal(existing, descriptor.source_config_sha256, actor.agent.team_id):
                return existing
            raise ValueError("An installed or pending MCP connection already uses this semantic name.")
        record = _new_record(
            descriptor,
            lifecycle=MCPConnectionLifecycle.PENDING,
            target_agent_types=normalized_targets,
        )
        self.store.save(record)
        LOGGER.info(
            "Agent proposed an inert MCP connection. event=mcp_connection_proposed "
            "connection_id=%s agent_id=%s transport=%s descriptor_sha256=%s",
            record.connection_id,
            actor.agent.agent_id,
            descriptor.transport.value,
            descriptor.descriptor_sha256,
        )
        return record

    @staticmethod
    def _proposable_transport(transport: str, *, allow_legacy_sse: bool) -> MCPConnectionTransport:
        normalized = normalize_mcp_transport(transport)
        if normalized == MCPConnectionTransport.STDIO:
            raise PermissionError(
                "Agents cannot propose stdio MCP commands; an operator-owned trusted handler is required."
            )
        if normalized == MCPConnectionTransport.LEGACY_SSE and not allow_legacy_sse:
            raise PermissionError("Legacy SSE proposals require the operator compatibility policy to be enabled.")
        return normalized

    @staticmethod
    def _target_types(target_agent_types: Sequence[str]) -> tuple[str, ...]:
        normalized = tuple(sorted({str(item or "").strip() for item in target_agent_types if str(item or "").strip()}))
        if not normalized or any(not re.fullmatch(r"[A-Za-z][A-Za-z0-9_.-]{0,119}", item) for item in normalized):
            raise ValueError("MCP target agent types must be exact valid agent type names.")
        return normalized

    def _live_record_named(self, connection_name: str) -> MCPConnectionRecordV1 | None:
        """The pending or active record with this name (names are unique among them)."""

        return next(
            (
                record
                for record in self.store.list_records()
                if record.descriptor.connection_name == connection_name and record.lifecycle in _LIVE_LIFECYCLES
            ),
            None,
        )

    @staticmethod
    def _same_proposal(existing: MCPConnectionRecordV1, source_config_sha256: str, team_id: str) -> bool:
        """The same agent-proposed configuration (its locator differs only by the proposal's nonce)."""

        descriptor = existing.descriptor
        return (
            descriptor.source == MCPConnectionSource.AGENT_PROPOSAL
            and descriptor.source_config_sha256 == source_config_sha256
            and descriptor.team_id == team_id
        )

    # -- the gateway's MCP controls (the person was checked by the gateway) --------

    @staticmethod
    def person_approval(person_id: str) -> ChangeApprovalRecordV1:
        """A person's direct approval in the gateway (no Inbox request)."""

        return ChangeApprovalRecordV1(
            mode=ChangeApprovalMode.PERSON_APPROVAL,
            approved_by=str(person_id),
            approved_at_utc=utc_now(),
        )

    def approve_by_person(self, connection_id: str, *, person_id: str) -> MCPConnectionRecordV1:
        """Activate a pending connection: it becomes the team tool ``mcp:<name>``.

        Who may use it is decided by grants; the gateway's MCP controls give
        the agent types the record names access when authorization is on
        (``access_control.mcp_servers.everyone_grants``).
        """

        record = self.store.require(connection_id)
        if record.lifecycle != MCPConnectionLifecycle.PENDING:
            raise PermissionError(
                "Only pending MCP connection definitions can be approved."
            )
        updated = self.store.update_lifecycle(
            record.connection_id,
            lifecycle=MCPConnectionLifecycle.ACTIVE,
            approval=self.person_approval(person_id),
        )
        LOGGER.info(
            "A person approved an MCP connection. event=mcp_connection_approved "
            "connection_id=%s person_id=%s",
            connection_id,
            person_id,
        )
        return updated

    def reject_by_person(self, connection_id: str, *, person_id: str, reason: str) -> MCPConnectionRecordV1:
        record = self.store.require(connection_id)
        if record.lifecycle != MCPConnectionLifecycle.PENDING:
            raise PermissionError(
                "Only pending MCP connection definitions can be rejected."
            )
        LOGGER.info(
            "MCP connection rejected. event=mcp_connection_rejected connection_id=%s person_id=%s",
            connection_id,
            person_id,
        )
        return self.store.update_lifecycle(
            record.connection_id,
            lifecycle=MCPConnectionLifecycle.REJECTED,
            reason=str(reason or "Rejected by a reviewer.").strip(),
        )

    def disable_by_person(self, connection_id: str, *, person_id: str) -> MCPConnectionRecordV1:
        record = self.store.require(connection_id)
        if record.lifecycle in {
            MCPConnectionLifecycle.DISABLED,
            MCPConnectionLifecycle.REMOVED,
        }:
            return record
        updated = self.store.update_lifecycle(
            connection_id,
            lifecycle=MCPConnectionLifecycle.DISABLED,
        )
        LOGGER.warning(
            "A person disabled an MCP connection. "
            "event=mcp_connection_disabled connection_id=%s person_id=%s",
            connection_id,
            person_id,
        )
        return updated

    def remove_by_person(self, connection_id: str, *, person_id: str) -> MCPConnectionRecordV1:
        record = self.store.require(connection_id)
        if record.lifecycle == MCPConnectionLifecycle.REMOVED:
            return record
        if record.lifecycle != MCPConnectionLifecycle.DISABLED:
            raise PermissionError(
                "Disable the MCP connection before recoverable removal."
            )
        removed = self.store.update_lifecycle(
            connection_id,
            lifecycle=MCPConnectionLifecycle.REMOVED,
        )
        LOGGER.warning(
            "A person recoverably removed an MCP connection. "
            "event=mcp_connection_removed connection_id=%s person_id=%s",
            connection_id,
            person_id,
        )
        return removed


def resolve_agent_mcp_connection_paths(agent: Any) -> MCPConnectionPaths:
    """The installed MCP connections of the agent's instance (``InstanceLayout.mcp_connections_dir``)."""

    instance_root = resolve_runtime_instance_root(
        instance_root=getattr(agent, "instance_root", None),
        bootstrap_config=dict(getattr(agent, "bootstrap_config", {}) or {}),
        instance_config_path=getattr(agent, "instance_config_path", None),
        workspace_dir=getattr(agent, "workspace_dir", None),
    )
    if not instance_root:
        raise RuntimeError("MCP connections require a resolvable instance root.")
    return resolve_mcp_connection_paths(instance_root)


__all__ = [
    "CONFIGURED_CONNECTION_IDS",
    "MCPConnectionAdministration",
    "apply_mcp_connection_reconciliation",
    "resolve_agent_mcp_connection_paths",
    "shown_without_authorization",
]
