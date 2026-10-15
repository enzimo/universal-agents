"""Mcp configuration and contracts."""

from __future__ import annotations
from typing import Any
from pydantic import BaseModel, Field, field_validator, model_validator
from mcp_control.registry import normalize_mcp_private_hosts


class MCPStaticReconciliationSettings(BaseModel):
    """Controls how desired static MCP definitions become installed authority."""

    upgrade_mode: str = "review_required"

    @field_validator("upgrade_mode", mode="before")
    @classmethod
    def _validate_upgrade_mode(cls, value: Any) -> str:
        normalized = str(value or "review_required").strip().lower()
        if normalized not in {"review_required", "grandfather_current"}:
            raise ValueError(
                "mcp.static_reconciliation.upgrade_mode must be "
                "'review_required' or 'grandfather_current'."
            )
        return normalized


class MCPNetworkPolicySettings(BaseModel):
    allow_loopback: bool = False
    allow_private_networks: bool = False
    allowed_private_hosts: tuple[str, ...] = ()

    @field_validator("allowed_private_hosts", mode="before")
    @classmethod
    def _normalize_allowed_private_hosts(cls, value: Any) -> tuple[str, ...]:
        return normalize_mcp_private_hosts(value or ())


class MCPLegacySSESettings(BaseModel):
    enabled: bool = True
    removal_target_date: str = "2026-11-12"


class MCPHostingSettings(BaseModel):
    # Definitions and state live in runtime/mcp-hosting/ (InstanceLayout);
    # the location is not a setting.
    enabled: bool = False
    bind_host: str = "127.0.0.1"
    port_range_start: int = Field(default=18700, ge=1024, le=65535)
    port_range_end: int = Field(default=18799, ge=1024, le=65535)

    @model_validator(mode="after")
    def _validate_port_range(self) -> "MCPHostingSettings":
        if self.port_range_end < self.port_range_start:
            raise ValueError("mcp.hosting.port_range_end must be >= port_range_start.")
        return self


class MCPSettings(BaseModel):
    """Installed MCP connection, transport, and managed-host settings."""

    # Installed connections live in runtime/mcp-connections/ (InstanceLayout);
    # the location is not a setting.
    enabled: bool = True
    protocol_adapter: str = "mcp_v1"
    # How long an agent keeps trying an installed server that does not answer
    # before it stops and tells administrators (custom_agents.mcp_server_tools);
    # at most a week, so an agent never retries without end.
    load_retry_window_minutes: int = Field(default=60, ge=1, le=10080)
    static_reconciliation: MCPStaticReconciliationSettings = Field(
        default_factory=MCPStaticReconciliationSettings
    )
    network_policy: MCPNetworkPolicySettings = Field(
        default_factory=MCPNetworkPolicySettings
    )
    legacy_sse: MCPLegacySSESettings = Field(default_factory=MCPLegacySSESettings)
    hosting: MCPHostingSettings = Field(default_factory=MCPHostingSettings)

    @field_validator("protocol_adapter", mode="before")
    @classmethod
    def _normalize_required_string(cls, value: Any) -> str:
        normalized = str(value or "").strip()
        if not normalized:
            raise ValueError("mcp.protocol_adapter must not be empty.")
        return normalized
