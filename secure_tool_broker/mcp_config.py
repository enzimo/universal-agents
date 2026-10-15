"""Broker-owned projection of operator MCP settings."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator

from mcp_control.registry import normalize_mcp_private_hosts


class BrokerConfigModel(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)


class BrokerAgentMCPSettings(BrokerConfigModel):
    external_mcp_servers: tuple[dict[str, Any], ...] = ()


class BrokerAgentMCPOverrideSettings(BrokerConfigModel):
    """Per-agent MCP override where ``None`` retains global inheritance."""

    external_mcp_servers: tuple[dict[str, Any], ...] | None = None


class BrokerAgentTypeMCPSettings(BrokerConfigModel):
    agent: BrokerAgentMCPOverrideSettings = Field(
        default_factory=BrokerAgentMCPOverrideSettings
    )


class BrokerMCPNetworkPolicy(BrokerConfigModel):
    allow_loopback: bool = False
    allow_private_networks: bool = False
    allowed_private_hosts: tuple[str, ...] = ()

    @field_validator("allowed_private_hosts", mode="before")
    @classmethod
    def _normalize_allowed_private_hosts(cls, value: Any) -> tuple[str, ...]:
        return normalize_mcp_private_hosts(value or ())


class BrokerMCPLegacySSEPolicy(BrokerConfigModel):
    enabled: bool = True


class BrokerMCPSettings(BrokerConfigModel):
    # MCP connection and hosting folders are derived from instance_root_path
    # (runtime_settings.instance_layout), so they are not part of this view.
    protocol_adapter: str = "mcp_v1"
    network_policy: BrokerMCPNetworkPolicy = Field(
        default_factory=BrokerMCPNetworkPolicy
    )
    legacy_sse: BrokerMCPLegacySSEPolicy = Field(
        default_factory=BrokerMCPLegacySSEPolicy
    )


class BrokerMCPConfig(BrokerConfigModel):
    """Only settings required to operate installed broker-owned MCP sessions."""

    instance_root_path: str
    agent: BrokerAgentMCPSettings = Field(default_factory=BrokerAgentMCPSettings)
    agent_types: dict[str, BrokerAgentTypeMCPSettings] = Field(default_factory=dict)
    mcp_profiles: dict[str, dict[str, Any]] = Field(default_factory=dict)
    mcp: BrokerMCPSettings = Field(default_factory=BrokerMCPSettings)

    @classmethod
    def from_runtime_settings(
        cls,
        *,
        instance_root_path: str,
        agent: BaseModel | Mapping[str, Any],
        agent_types: Mapping[str, BaseModel | Mapping[str, Any]],
        mcp_profiles: Mapping[str, Mapping[str, Any]],
        mcp: BaseModel | Mapping[str, Any],
    ) -> "BrokerMCPConfig":
        """Project the canonical runtime settings onto the broker-owned view."""

        return cls(
            instance_root_path=instance_root_path,
            agent=_config_payload(agent),
            agent_types={
                name: _config_payload(settings)
                for name, settings in agent_types.items()
            },
            mcp_profiles={
                name: dict(settings) for name, settings in mcp_profiles.items()
            },
            mcp=_config_payload(mcp),
        )


def _config_payload(value: BaseModel | Mapping[str, Any]) -> dict[str, Any]:
    if isinstance(value, BaseModel):
        return value.model_dump(mode="python")
    return dict(value)
