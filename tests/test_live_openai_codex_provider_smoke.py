"""Opt-in live smoke tests for the OpenAI Codex OAuth provider.

These tests hit live OpenAI Codex endpoints and are skipped unless
RUN_LIVE_OPENAI_CODEX_PROVIDER_SMOKE=1 is set.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any

import pytest
from pydantic import BaseModel
from strands import Agent, tool

from assistant_gateway.config import GatewayInstanceConfig
from config_loader import load_config, resolve_agent_type_settings, set_config
from custom_agents.openai_codex_auth import OpenAICodexAuthError, resolve_openai_codex_credentials
from custom_agents.openai_codex_models import (
    get_codex_model_ids,
    resolve_openai_codex_model_discovery_settings,
)
from custom_agents.openai_reasoning_responses_model import OpenAIReasoningResponsesModel
from custom_agents.strands_model_factory import StrandsModelFactoryConfig, build_strands_model
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
        os.getenv("RUN_LIVE_OPENAI_CODEX_PROVIDER_SMOKE") != "1",
        reason="Set RUN_LIVE_OPENAI_CODEX_PROVIDER_SMOKE=1 to run live OpenAI Codex provider smoke tests.",
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
    forced_model_id = str(os.getenv("TEAM_NATS_OPENAI_CODEX_SMOKE_MODEL_ID") or "").strip()
    if settings.get("model_provider") != "openai-codex" and not forced_model_id:
        pytest.skip(f"Configured provider is not openai-codex: {settings.get('model_provider')!r}")
    if forced_model_id:
        settings["model_id"] = forced_model_id
    settings["instance_root"] = instance_root
    return settings


def test_live_openai_codex_model_discovery_and_strands_call() -> None:
    settings = _runtime_settings()
    try:
        credentials = resolve_openai_codex_credentials(
            settings.get("openai_codex"),
            instance_root=settings.get("instance_root"),
            workspace_dir=settings.get("workspace_dir"),
        )
    except OpenAICodexAuthError as exc:
        pytest.skip(f"OpenAI Codex credentials unavailable: {exc}")

    discovery_settings = resolve_openai_codex_model_discovery_settings(
        settings.get("openai_codex"),
        instance_root=settings.get("instance_root"),
        workspace_dir=settings.get("workspace_dir"),
    )
    models = get_codex_model_ids(discovery_settings, access_token=credentials.api_key, force_refresh=True)
    assert settings["model_id"] in models or models

    model = build_strands_model(
        StrandsModelFactoryConfig(
            provider="openai-codex",
            model_id=settings["model_id"],
            temperature=0.0,
            reasoning_effort=settings.get("reasoning_effort"),
            reasoning_summary=settings.get("reasoning_summary"),
            max_output_tokens=64,
            openai_codex=settings.get("openai_codex"),
            instance_root=settings.get("instance_root"),
            workspace_dir=settings.get("workspace_dir"),
        )
    )
    request_params = model.config.get("params") or {}
    assert "max_output_tokens" not in request_params
    assert "temperature" not in request_params
    if settings.get("reasoning_summary"):
        assert request_params["reasoning"]["summary"] == settings["reasoning_summary"]
    agent = Agent(system_prompt="You are a terse test agent.", model=model, tools=[])
    result = agent("Return answer as OK.", structured_output_model=TinyResponse)
    assert result.structured_output == TinyResponse(answer="OK")


def test_live_openai_codex_reasoning_tool_loop_replays_raw_output(caplog) -> None:
    settings = _runtime_settings()
    try:
        credentials = resolve_openai_codex_credentials(
            settings.get("openai_codex"),
            instance_root=settings.get("instance_root"),
            workspace_dir=settings.get("workspace_dir"),
        )
    except OpenAICodexAuthError as exc:
        pytest.skip(f"OpenAI Codex credentials unavailable: {exc}")

    tool_calls: list[str] = []

    @tool
    def codex_reasoning_replay_probe(value: str) -> str:
        """Return the exact supplied compatibility-probe value."""
        tool_calls.append(value)
        return value

    model = build_strands_model(
        StrandsModelFactoryConfig(
            provider="openai-codex",
            model_id=settings["model_id"],
            reasoning_effort="xhigh",
            reasoning_summary="detailed",
            openai_codex=settings.get("openai_codex"),
            instance_root=settings.get("instance_root"),
            workspace_dir=settings.get("workspace_dir"),
        )
    )
    assert isinstance(model, OpenAIReasoningResponsesModel)
    assert model.client_args["api_key"] == credentials.api_key

    replayed_reasoning_batches: list[list[dict[str, Any]]] = []
    original_formatter = model._format_request_messages_with_state

    def capture_formatted_input(messages, model_state):
        formatted = original_formatter(messages, model_state)
        reasoning_items = [
            item
            for item in formatted
            if item.get("type") == "reasoning" and item.get("encrypted_content")
        ]
        if reasoning_items:
            replayed_reasoning_batches.append(reasoning_items)
        return formatted

    model._format_request_messages_with_state = capture_formatted_input
    agent = Agent(
        model=model,
        tools=[codex_reasoning_replay_probe],
        system_prompt=(
            "Determine which of 143, 187, 210, and 221 has the most distinct "
            "prime factors. Check each factorization, then call "
            "codex_reasoning_replay_probe exactly once with value "
            "codex-replay-ok and reply with exactly codex-replay-ok."
        ),
    )

    with caplog.at_level(logging.WARNING):
        result = agent(
            "Determine which candidate has the most distinct prime factors, "
            "use the required probe exactly once, and return the required value."
        )

    assert tool_calls == ["codex-replay-ok"]
    assert "codex-replay-ok" in str(result).lower()
    assert replayed_reasoning_batches
    assert "reasoningContent is not yet supported" not in caplog.text
    assert "event=openai_reasoning_replay_missing" not in caplog.text
    assert "event=openai_reasoning_output_index_missing" not in caplog.text
    assert not any(
        "event=openai_reasoning_output_sources_mismatch" in record.message
        and "material=True" in record.message
        for record in caplog.records
    )
