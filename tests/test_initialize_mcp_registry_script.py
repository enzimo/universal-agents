from __future__ import annotations

from pathlib import Path

import pytest

from assistant_gateway.config import GatewayInstanceConfig
from scripts.initialize_mcp_registry import initialize_mcp_registry, main


def _instance_config(tmp_path: Path) -> Path:
    root = tmp_path / "instance"
    config = GatewayInstanceConfig.create(
        instance_name="mcp-initializer-test",
        instance_root=str(root),
        team_id="team_mcp_initializer",
        timezone="UTC",
    )
    config.agent.external_mcp_servers = [
        {
            "name": "test-service",
            "transport": "streamable_http",
            "server_url": "https://mcp.example.test/mcp",
            "tool_name_prefix": "test_service",
        }
    ]
    return config.save(root / "instance_config.yaml")


def test_initialize_mcp_registry_creates_and_reuses_baseline(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_path = _instance_config(tmp_path)
    monkeypatch.setattr(
        "socket.getaddrinfo",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("MCP registry initialization attempted network access")
        ),
    )

    first = initialize_mcp_registry(config_path)
    second = initialize_mcp_registry(config_path)

    assert first.baseline_path.is_file()
    assert first.baseline_preexisting is False
    assert first.connection_count == 1
    assert second.baseline_path == first.baseline_path
    assert second.baseline_preexisting is True
    assert second.connection_count == 1


def test_initialize_mcp_registry_cli_requires_exclusive_confirmation(
    tmp_path: Path,
) -> None:
    config_path = _instance_config(tmp_path)

    with pytest.raises(SystemExit) as exc_info:
        main(["--config", str(config_path)])

    assert exc_info.value.code == 2
