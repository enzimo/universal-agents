"""Opt-in live validation for native OpenAI Responses reasoning and tools."""

from __future__ import annotations

from tests.model_key_support import env_model_keys

import os
from pathlib import Path

import pytest
from openai.types.responses.response_create_params import ResponseCreateParamsStreaming
from strands import Agent, tool

from custom_agents.filtered_tool_result_context import FilteredToolResultConversationManager
from custom_agents.strands_model_factory import StrandsModelFactoryConfig, build_strands_model

try:
    from dotenv import load_dotenv
except ImportError:  # pragma: no cover - only relevant in stripped environments
    load_dotenv = None


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_ENV_PATH = PROJECT_ROOT / "debug.env"

pytestmark = [
    pytest.mark.live,
    pytest.mark.external,
    pytest.mark.skipif(
        os.getenv("RUN_LIVE_OPENAI_PROVIDER_SMOKE") != "1",
        reason="Set RUN_LIVE_OPENAI_PROVIDER_SMOKE=1 to run the native OpenAI provider smoke test.",
    ),
]


def _load_openai_api_key() -> str:
    env_path = Path(os.getenv("TEAM_NATS_LIVE_ENV", str(DEFAULT_ENV_PATH))).expanduser()
    if env_path.is_file() and load_dotenv is not None:
        load_dotenv(env_path, override=False)
    api_key = str(os.getenv("OPENAI_API_KEY") or "").strip()
    if not api_key:
        pytest.skip("OPENAI_API_KEY is unavailable.")
    return api_key


def test_live_native_openai_reasoning_tool_loop_uses_supported_schema(
    tmp_path: Path,
    caplog,
) -> None:
    tool_calls: list[str] = []

    @tool
    def live_schema_probe(value: str) -> str:
        """Return the supplied schema-probe value unchanged."""
        tool_calls.append(value)
        return value

    model_id = str(os.getenv("TEAM_NATS_OPENAI_SMOKE_MODEL_ID") or "gpt-5.6-luna").strip()
    model = build_strands_model(
        StrandsModelFactoryConfig(
            provider="openai",
            model_id=model_id,
            temperature=0.0,
            reasoning_effort="medium",
            reasoning_summary="auto",
            max_output_tokens=512,
            model_keys=env_model_keys(_load_openai_api_key(), env_var="OPENAI_API_KEY"),
        )
    )
    request = model._format_request(  # noqa: SLF001 - verifies the provider wire contract.
        [
            {
                "role": "user",
                "content": [
                    {
                        "text": (
                            "Call live_schema_probe with value schema-ok, then reply exactly schema-ok."
                        )
                    }
                ],
            }
        ],
        [live_schema_probe.tool_spec],
        "You must use the provided function tool exactly once.",
        model_state={},
    )

    assert set(request) == {
        "include",
        "input",
        "instructions",
        "max_output_tokens",
        "model",
        "reasoning",
        "store",
        "stream",
        "tools",
    }
    assert set(request) <= set(ResponseCreateParamsStreaming.__annotations__)
    assert request["reasoning"] == {"effort": "medium", "summary": "auto"}
    assert request["include"] == ["reasoning.encrypted_content"]
    assert request["store"] is False
    assert request["tools"][0]["type"] == "function"
    assert "temperature" not in request
    assert "reasoning_effort" not in request
    assert "max_completion_tokens" not in request
    assert "previous_response_id" not in request

    conversation_manager = FilteredToolResultConversationManager(
        workspace_dir=tmp_path,
        model=model,
    )
    agent = Agent(
        model=model,
        tools=[live_schema_probe],
        system_prompt="You must use live_schema_probe exactly once and then return its value.",
        conversation_manager=conversation_manager,
    )

    result = agent(
        "Call live_schema_probe with value schema-ok, then reply exactly schema-ok."
    )

    assert tool_calls == ["schema-ok"]
    assert "schema-ok" in str(result).lower()
    assert "reasoningContent is not yet supported" not in caplog.text
    assert "event=openai_reasoning_replay_missing" not in caplog.text
