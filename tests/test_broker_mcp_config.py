from __future__ import annotations

import pytest
from pydantic import ValidationError

from secure_tool_broker.mcp_config import BrokerMCPConfig


def test_agent_type_mcp_overrides_preserve_inheritance_and_explicit_values() -> None:
    configured_server = {
        "name": "research",
        "transport": "streamable_http",
        "server_url": "https://mcp.example.test/mcp",
    }

    config = BrokerMCPConfig(
        instance_root_path="/tmp/ua-instance",
        agent_types={
            "InheritedAgent": {"agent": {"external_mcp_servers": None}},
            "ExplicitEmptyAgent": {"agent": {"external_mcp_servers": []}},
            "ConfiguredAgent": {
                "agent": {"external_mcp_servers": [configured_server]}
            },
        },
    )

    assert config.agent_types["InheritedAgent"].agent.external_mcp_servers is None
    assert config.agent_types["ExplicitEmptyAgent"].agent.external_mcp_servers == ()
    assert config.agent_types["ConfiguredAgent"].agent.external_mcp_servers == (
        configured_server,
    )


def test_top_level_mcp_servers_remain_non_nullable() -> None:
    config = BrokerMCPConfig(instance_root_path="/tmp/ua-instance")

    assert config.agent.external_mcp_servers == ()
    with pytest.raises(ValidationError):
        BrokerMCPConfig(
            instance_root_path="/tmp/ua-instance",
            agent={"external_mcp_servers": None},
        )


def test_private_host_allowlist_is_normalized_in_broker_projection() -> None:
    config = BrokerMCPConfig(
        instance_root_path="/tmp/ua-instance",
        mcp={
            "network_policy": {
                "allow_private_networks": False,
                "allowed_private_hosts": ["Crawl4AI.", "crawl4ai"],
            }
        },
    )

    assert config.mcp.network_policy.allowed_private_hosts == ("crawl4ai",)

    with pytest.raises(ValidationError, match="list of exact hosts"):
        BrokerMCPConfig(
            instance_root_path="/tmp/ua-instance",
            mcp={"network_policy": {"allowed_private_hosts": "crawl4ai"}},
        )
