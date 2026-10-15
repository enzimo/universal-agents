"""Opt-in live smoke test for the xAI OAuth provider.

This test calls live xAI endpoints and is skipped unless
RUN_LIVE_XAI_OAUTH_PROVIDER_SMOKE=1 is set.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import pytest
from pydantic import BaseModel
from strands import Agent

from assistant_gateway.config import GatewayInstanceConfig
from config_loader import load_config, resolve_agent_type_settings, set_config
from custom_agents.strands_model_factory import StrandsModelFactoryConfig, build_strands_model
from custom_agents.xai_oauth_auth import (
    DEFAULT_XAI_OAUTH_MODEL,
    XAIOAuthError,
    is_xai_oauth_provider,
    resolve_xai_oauth_credentials,
)
from strands_main import _build_runtime_config_from_instance_config

try:
    from dotenv import load_dotenv
except ImportError:  # pragma: no cover - only relevant in stripped envs
    load_dotenv = None


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_ENV_PATH = PROJECT_ROOT / "debug.env"
DEFAULT_CONFIG_PATH = PROJECT_ROOT / "config.yaml"
DEFAULT_INSTANCE_CONFIG_PATH = PROJECT_ROOT / "ignoreme" / "instances" / "live" / "instance_config.yaml"

pytestmark = [
    pytest.mark.live,
    pytest.mark.external,
    pytest.mark.skipif(
        os.getenv("RUN_LIVE_XAI_OAUTH_PROVIDER_SMOKE") != "1",
        reason="Set RUN_LIVE_XAI_OAUTH_PROVIDER_SMOKE=1 to run the live xAI OAuth provider smoke test.",
    ),
]


class TinyResponse(BaseModel):
    answer: str


def _load_live_env() -> None:
    env_path = Path(os.getenv("TEAM_NATS_LIVE_ENV", str(DEFAULT_ENV_PATH))).expanduser()
    if env_path.exists() and load_dotenv is not None:
        load_dotenv(env_path, override=True)


def _runtime_settings() -> dict[str, Any]:
    _load_live_env()
    instance_config_path = Path(
        os.getenv("TEAM_NATS_PROVIDER_SMOKE_INSTANCE_CONFIG", str(DEFAULT_INSTANCE_CONFIG_PATH))
    ).expanduser()
    config_path = Path(os.getenv("TEAM_NATS_PROVIDER_SMOKE_CONFIG", str(DEFAULT_CONFIG_PATH))).expanduser()
    if instance_config_path.exists():
        config = _build_runtime_config_from_instance_config(GatewayInstanceConfig.load(instance_config_path))
        instance_root = str(instance_config_path.resolve().parent)
    elif config_path.exists():
        config = load_config(str(config_path), search_default_paths=False)
        instance_root = None
    else:
        pytest.skip(
            "Neither live smoke instance config nor config.yaml was found: "
            f"{instance_config_path}, {config_path}"
        )
    set_config(config)
    settings = resolve_agent_type_settings(config, "TeamArchitect")
    if not is_xai_oauth_provider(settings.get("model_provider")):
        pytest.skip(f"Configured provider is not xai-oauth: {settings.get('model_provider')!r}")
    settings["instance_root"] = instance_root
    return settings


def test_live_xai_oauth_strands_structured_output() -> None:
    settings = _runtime_settings()
    model_id = str(settings.get("model_id") or DEFAULT_XAI_OAUTH_MODEL)
    try:
        resolve_xai_oauth_credentials(
            settings.get("xai_oauth"),
            instance_root=settings.get("instance_root"),
            workspace_dir=settings.get("workspace_dir"),
        )
    except XAIOAuthError as exc:
        pytest.skip(f"xAI OAuth credentials unavailable: {exc}")

    model = build_strands_model(
        StrandsModelFactoryConfig(
            provider="xai-oauth",
            model_id=model_id,
            temperature=0.0,
            max_output_tokens=64,
            xai_oauth=settings.get("xai_oauth"),
            instance_root=settings.get("instance_root"),
            workspace_dir=settings.get("workspace_dir"),
        )
    )
    assert model.config["model_id"] == model_id
    assert model.config["stateful"] is False

    agent = Agent(system_prompt="You are a terse test agent.", model=model, tools=[])
    result = agent("Return answer as OK.", structured_output_model=TinyResponse)
    assert result.structured_output is not None
    assert result.structured_output.answer.strip().upper() == "OK"
