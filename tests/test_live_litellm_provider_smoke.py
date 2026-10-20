"""Opt-in live smoke tests for configured LiteLLM Chat and Responses providers.

These tests preserve provider/proxy/Strands checks for configured LiteLLM
models, including Fireworks compatibility cases. They intentionally hit live
LLM endpoints and are skipped unless RUN_LIVE_LITELLM_PROVIDER_SMOKE=1 is set.

The model key comes the way the runtime gets it (``custom_agents.model_keys``):
every model is built with a ``ModelKeys`` lookup, never a fixed key, and the
key is the ``LITELLM_API_KEY`` environment override for the endpoint's own
address. The test reads it from the env file named by ``TEAM_NATS_LIVE_ENV``
(default ``debug.env``), then from this process's environment. The file is read,
not loaded into the environment: an instance's ``team.env`` may still hold
variables startup refuses (``runtime_settings.security``), and only the key is
needed. The broker's stored keys are not used, so no broker has to run.
"""

from __future__ import annotations

from tests.llm_turn_fixtures import turn_executor_for

import asyncio
import os
import struct
import zlib
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
import requests
from pydantic import BaseModel
from strands import Agent, tool

from assistant_gateway.runtime_config import load_runtime_config_file
from config_loader import resolve_agent_type_settings, set_config
from custom_agents.sdk.strands import turn as llm_turn_runtime
from custom_agents.runtime_model_switch import probe_strands_model
from custom_agents.runtime_guardrails import RuntimeGuardrailsPlugin
from custom_agents.model_keys import ModelKeyError, ModelKeys
from custom_agents.strands_model_factory import StrandsModelFactoryConfig, build_strands_model
from custom_agents.strands_team import AGENT_TYPE_MAP, build_system_prompt
from custom_agents.structured_llm_messages import (
    CompatibleModelAgentTurnDecision,
    DelegateTaskAction,
    FireworksModelAgentTurnDecision,
    ModelAgentTurnDecision,
    TaskResponseAction,
)
from custom_messages.enums import MessageMetaType
from custom_tools.providers.runtime_settings import plan_runtime_settings_change
from model_providers.endpoints import openai_compatible_endpoint

try:
    from dotenv import dotenv_values
except ImportError:  # pragma: no cover - only relevant in stripped envs
    dotenv_values = None


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_ENV_PATH = PROJECT_ROOT / "debug.env"
DEFAULT_CONFIG_PATH = PROJECT_ROOT / "config.yaml"
DEFAULT_INSTANCE_CONFIG_PATH = PROJECT_ROOT / "ignoreme" / "instances" / "live" / "instance_config.yaml"
FIREWORKS_DEEPSEEK_V4_FLASH_MODEL_ID = "fireworks/deepseek-v4-flash"
FIREWORKS_RAW_DEEPSEEK_V4_FLASH_MODEL_ID = "deepseek-v4-flash-0731"
FIREWORKS_MINIMAX_M3_MODEL_ID = "fireworks/minimax-m3"
DEFAULT_RESPONSES_SMOKE_MODEL_ID = "openai/gpt-5.6-luna"
_TRUE_VALUES = {"1", "true", "yes", "on"}

responses_route_smoke = pytest.mark.skipif(
    os.getenv("RUN_LIVE_LITELLM_RESPONSES_SMOKE", "").strip().lower()
    not in _TRUE_VALUES,
    reason="Set RUN_LIVE_LITELLM_RESPONSES_SMOKE=1 for the explicit Responses route.",
)
fireworks_deepseek_smoke = pytest.mark.skipif(
    os.getenv("RUN_LIVE_LITELLM_FIREWORKS_DEEPSEEK_SMOKE", "").strip().lower()
    not in _TRUE_VALUES,
    reason=(
        "Set RUN_LIVE_LITELLM_FIREWORKS_DEEPSEEK_SMOKE=1 for the optional "
        "Fireworks DeepSeek routes."
    ),
)
fireworks_minimax_smoke = pytest.mark.skipif(
    os.getenv("RUN_LIVE_LITELLM_FIREWORKS_MINIMAX_SMOKE", "").strip().lower()
    not in _TRUE_VALUES,
    reason=(
        "Set RUN_LIVE_LITELLM_FIREWORKS_MINIMAX_SMOKE=1 for the optional "
        "Fireworks MiniMax route."
    ),
)

pytestmark = [
    pytest.mark.live,
    pytest.mark.external,
    pytest.mark.skipif(
        os.getenv("RUN_LIVE_LITELLM_PROVIDER_SMOKE") != "1",
        reason="Set RUN_LIVE_LITELLM_PROVIDER_SMOKE=1 to run live LiteLLM provider smoke tests.",
    ),
]


class TinyResponse(BaseModel):
    answer: str


def _live_env_value(name: str) -> str | None:
    """A value from the live env file, else this process's environment (module docstring)."""

    env_path = Path(os.getenv("TEAM_NATS_LIVE_ENV", str(DEFAULT_ENV_PATH))).expanduser()
    if env_path.is_file() and dotenv_values is not None:
        value = str(dotenv_values(env_path).get(name) or "").strip()
        if value:
            return value
    return str(os.getenv(name) or "").strip() or None


def _live_model_keys() -> ModelKeys:
    """The runtime's key lookup with only environment overrides (no broker)."""

    return ModelKeys(None, env_lookup=_live_env_value)


def _api_key(settings: dict[str, Any]) -> str:
    """The key the runtime would send to the configured endpoint."""

    return _live_model_keys().resolve(openai_compatible_endpoint(settings["litellm_api_base"])).value


def _chat_completions_url(api_base: str) -> str:
    """Chat Completions on the configured base, which may or may not already end in /v1."""

    base = api_base.rstrip("/")
    return f"{base}/chat/completions" if base.endswith("/v1") else f"{base}/v1/chat/completions"


def _runtime_settings(
    *,
    model_id_override: str | None = None,
    prefer_instance_config: bool = True,
) -> dict[str, Any]:
    explicit_api_base = str(os.getenv("TEAM_NATS_PROVIDER_SMOKE_API_BASE") or "").strip()
    explicit_instance_config = os.getenv("TEAM_NATS_PROVIDER_SMOKE_INSTANCE_CONFIG")
    explicit_config = os.getenv("TEAM_NATS_PROVIDER_SMOKE_CONFIG")
    shared_live_config = os.getenv("TEAM_NATS_LIVE_CONFIG") or os.getenv(
        "UA_LIVE_CONFIG"
    )
    if explicit_api_base:
        settings = {
            "model_provider": "litellm",
            "model_id": str(os.getenv("TEAM_NATS_PROVIDER_SMOKE_MODEL_ID") or "").strip(),
            "litellm_api_base": explicit_api_base,
            "temperature": 0.0,
            "request_timeout_seconds": 120,
        }
    else:
        if explicit_instance_config:
            candidates = [Path(explicit_instance_config)]
        elif explicit_config:
            candidates = [Path(explicit_config)]
        elif shared_live_config:
            candidates = [Path(shared_live_config)]
        elif prefer_instance_config:
            candidates = [DEFAULT_INSTANCE_CONFIG_PATH, DEFAULT_CONFIG_PATH]
        else:
            candidates = [DEFAULT_CONFIG_PATH, DEFAULT_INSTANCE_CONFIG_PATH]
        config_path = next(
            (
                candidate.expanduser().resolve()
                for candidate in candidates
                if candidate.expanduser().is_file()
            ),
            None,
        )
        if config_path is None:
            rendered = ", ".join(str(candidate.expanduser()) for candidate in candidates)
            pytest.skip(f"No live provider config was found in: {rendered}")
        config = load_runtime_config_file(config_path)
        set_config(config)
        settings = resolve_agent_type_settings(config, "TeamArchitect")
    if settings.get("model_provider") != "litellm":
        pytest.skip(f"Configured provider is not litellm: {settings.get('model_provider')!r}")
    if not settings.get("litellm_api_base"):
        pytest.skip("Configured litellm_api_base is empty.")
    try:
        _api_key(settings)
    except ModelKeyError as exc:
        pytest.skip(f"No model key for {settings['litellm_api_base']}: set LITELLM_API_KEY in the live env file. {exc}")
    if model_id_override:
        settings = {**settings, "model_id": model_id_override}
    elif os.getenv("TEAM_NATS_PROVIDER_SMOKE_MODEL_ID"):
        settings = {**settings, "model_id": os.getenv("TEAM_NATS_PROVIDER_SMOKE_MODEL_ID")}
    return settings


def _litellm_model(
    *,
    stream: bool | None,
    max_tokens: int = 300,
    settings: dict[str, Any] | None = None,
) -> Any:
    settings = settings or _runtime_settings()
    return build_strands_model(
        StrandsModelFactoryConfig(
            provider="litellm",
            model_id=settings["model_id"],
            temperature=settings.get("temperature"),
            reasoning_effort=settings.get("reasoning_effort"),
            reasoning_summary=settings.get("reasoning_summary"),
            max_output_tokens=max_tokens,
            context_window_tokens=settings.get("context_window_tokens"),
            stream=stream,
            litellm_api_mode=settings.get("litellm_api_mode", "chat_completions"),
            request_timeout_seconds=settings.get("request_timeout_seconds"),
            reasoning_trace_retention_messages=settings.get(
                "reasoning_trace_retention_messages"
            ),
            litellm_api_base=settings["litellm_api_base"],
            instance_root=settings.get("instance_root"),
            model_keys=_live_model_keys(),
        )
    )


def _chat_request_params(settings: dict[str, Any], *, max_tokens: int) -> dict[str, Any]:
    model = _litellm_model(stream=False, max_tokens=max_tokens, settings=settings)
    config = getattr(model, "config", {})
    params = dict(config.get("params") or {})
    params.pop("timeout", None)
    return params


def _manager_system_prompt() -> str:
    manager = AGENT_TYPE_MAP["TeamArchitect"]
    return build_system_prompt(
        manager["role"],
        manager["goal"],
        manager["backstory"],
        "team_303",
    )


def _turn_decision_output_model(
    settings: dict[str, Any] | None = None,
) -> type[ModelAgentTurnDecision] | type[CompatibleModelAgentTurnDecision]:
    settings = settings or _runtime_settings()
    return turn_executor_for(
        SimpleNamespace(
            model_provider=settings["model_provider"],
            model_id=settings["model_id"],
            litellm_api_base=settings["litellm_api_base"],
        )
    )._turn_decision_structured_output_model()


def _turn_decision_output_prompt(settings: dict[str, Any] | None = None) -> str:
    settings = settings or _runtime_settings()
    return turn_executor_for(
        SimpleNamespace(
            model_provider=settings["model_provider"],
            model_id=settings["model_id"],
            litellm_api_base=settings["litellm_api_base"],
        )
    )._turn_decision_structured_output_prompt()


def _post_litellm_chat_completion(settings: dict[str, Any], prompt: str) -> requests.Response:
    if settings.get("litellm_api_mode", "chat_completions") != "chat_completions":
        pytest.skip("The configured LiteLLM route does not use Chat Completions.")
    return requests.post(
        _chat_completions_url(settings["litellm_api_base"]),
        headers={"Authorization": f"Bearer {_api_key(settings)}"},
        json={
            "model": settings["model_id"],
            "messages": [{"role": "user", "content": prompt}],
            **_chat_request_params(settings, max_tokens=200),
        },
        timeout=45,
    )


def _assert_ok_text(value: object) -> None:
    rendered = str(value).strip()
    assert "OK" in rendered


async def _collect_response_stream(model: Any) -> tuple[str, str]:
    text_parts: list[str] = []
    reasoning_parts: list[str] = []
    async for event in model.stream(
        messages=[
            {
                "role": "user",
                "content": [
                    {
                        "text": (
                            "Solve the alphametic SEND + MORE = MONEY, where each letter "
                            "is a distinct decimal digit and S and M are nonzero. Check "
                            "every column and carry, then end your answer with exactly OK."
                        )
                    }
                ],
            }
        ],
        tool_specs=[],
        model_state={},
    ):
        delta = event.get("contentBlockDelta", {}).get("delta", {})
        if isinstance(delta.get("text"), str):
            text_parts.append(delta["text"])
        reasoning = delta.get("reasoningContent")
        if isinstance(reasoning, dict) and isinstance(reasoning.get("text"), str):
            reasoning_parts.append(reasoning["text"])
    return "".join(text_parts).strip(), "".join(reasoning_parts).strip()


def _answer_text(value: object) -> str:
    if hasattr(value, "answer"):
        return str(getattr(value, "answer"))
    return str(value)


def _png_with_red_center_on_blue_background(width: int = 96, height: int = 96) -> bytes:
    rows = []
    for y in range(height):
        row = bytearray()
        for x in range(width):
            row.extend((255, 0, 0) if 16 <= x < 80 and 16 <= y < 80 else (0, 0, 255))
        rows.append(b"\x00" + bytes(row))
    raw = b"".join(rows)

    def chunk(name: bytes, data: bytes) -> bytes:
        checksum = zlib.crc32(name + data) & 0xFFFFFFFF
        return struct.pack(">I", len(data)) + name + data + struct.pack(">I", checksum)

    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(raw))
        + chunk(b"IEND", b"")
    )


async def _strands_image_block_answer(settings: dict[str, Any]) -> tuple[str, str]:
    model = _litellm_model(stream=None, max_tokens=64, settings=settings)
    messages = [
        {
            "role": "user",
            "content": [
                {"text": "What color is the central square in this image? Answer with one word."},
                {
                    "image": {
                        "format": "png",
                        "source": {"bytes": _png_with_red_center_on_blue_background()},
                    }
                },
            ],
        }
    ]
    text_parts: list[str] = []
    reasoning_parts: list[str] = []
    async for event in model.stream(messages=messages, tool_specs=[]):
        delta = event.get("contentBlockDelta", {}).get("delta", {})
        if "text" in delta:
            text_parts.append(str(delta["text"]))
        reasoning = delta.get("reasoningContent")
        if isinstance(reasoning, dict) and reasoning.get("text"):
            reasoning_parts.append(str(reasoning["text"]))
    return "".join(text_parts).strip(), "".join(reasoning_parts).strip()


def _assert_turn_decision_answer(result: object, expected_text: str) -> None:
    decision = getattr(result, "structured_output", None)
    assert isinstance(decision, (ModelAgentTurnDecision, FireworksModelAgentTurnDecision))
    runtime_decision = decision.to_runtime_decision()
    assert len(runtime_decision.actions) == 1
    action = runtime_decision.actions[0]
    assert isinstance(action, TaskResponseAction)
    assert action.task.task_type == MessageMetaType.RESPONSE
    assert action.task.task_id is None
    assert action.task.parent_task_id is None
    assert expected_text in _answer_text(action.task.answer)


def _assert_turn_decision_delegation(result: object, *, expected_target: str) -> None:
    decision = getattr(result, "structured_output", None)
    assert isinstance(decision, (ModelAgentTurnDecision, FireworksModelAgentTurnDecision))
    runtime_decision = decision.to_runtime_decision()
    assert len(runtime_decision.actions) == 1
    action = runtime_decision.actions[0]
    assert isinstance(action, DelegateTaskAction)
    assert action.target_named_agent_type == expected_target
    assert action.requester_type is None


def test_live_litellm_provider_resolves_current_instance_model() -> None:
    settings = _runtime_settings()

    assert settings["model_provider"] == "litellm"
    assert settings["model_id"]
    assert settings["litellm_api_base"].startswith(("http://", "https://"))


def test_live_litellm_proxy_non_streaming_chat_completion_works() -> None:
    settings = _runtime_settings()
    response = _post_litellm_chat_completion(settings, "Reply with exactly OK")

    assert response.status_code == 200, response.text
    payload = response.json()
    _assert_ok_text(payload["choices"][0]["message"]["content"])


@responses_route_smoke
def test_live_litellm_responses_stream_emits_openai_reasoning_summary() -> None:
    model_id = str(
        os.getenv("TEAM_NATS_RESPONSES_SMOKE_MODEL_ID")
        or DEFAULT_RESPONSES_SMOKE_MODEL_ID
    ).strip()
    settings = _runtime_settings(
        model_id_override=model_id,
        prefer_instance_config=False,
    )
    model = build_strands_model(
        StrandsModelFactoryConfig(
            provider="litellm",
            model_id=model_id,
            reasoning_effort="xhigh",
            reasoning_summary="detailed",
            max_output_tokens=4_096,
            litellm_api_mode="responses",
            litellm_api_base=settings["litellm_api_base"],
            request_timeout_seconds=120,
            model_keys=_live_model_keys(),
        )
    )

    answer, reasoning = asyncio.run(_collect_response_stream(model))

    assert reasoning
    _assert_ok_text(answer)


@fireworks_deepseek_smoke
def test_live_litellm_fireworks_deepseek_v4_flash_proxy_non_streaming_chat_completion_works() -> None:
    settings = _runtime_settings(
        model_id_override=FIREWORKS_DEEPSEEK_V4_FLASH_MODEL_ID,
        prefer_instance_config=False,
    )

    assert settings["model_id"] == FIREWORKS_DEEPSEEK_V4_FLASH_MODEL_ID
    response = _post_litellm_chat_completion(settings, "Reply with exactly OK")

    assert response.status_code == 200, response.text
    payload = response.json()
    _assert_ok_text(payload["choices"][0]["message"]["content"])


def test_live_litellm_minimal_strands_plain_and_structured_calls_work() -> None:
    model = _litellm_model(stream=None)
    agent = Agent(system_prompt="You are a terse test agent.", model=model, tools=[])

    plain_result = agent("Reply with exactly the text OK and nothing else.")
    structured_result = agent("Return answer as OK.", structured_output_model=TinyResponse)

    _assert_ok_text(plain_result)
    assert structured_result.structured_output == TinyResponse(answer="OK")


def test_live_litellm_factory_request_matches_configured_model() -> None:
    settings = _runtime_settings(prefer_instance_config=False)
    model = _litellm_model(stream=None, max_tokens=128, settings=settings)
    messages = [{"role": "user", "content": [{"text": "Reply exactly OK"}]}]
    if settings.get("litellm_api_mode") == "responses":
        request = model._format_request(  # noqa: SLF001 - verifies the live wire contract.
            messages,
            [],
            system_prompt="Reply exactly as requested.",
            model_state={},
        )
        assert request["max_output_tokens"] == 128
        assert "temperature" not in request
    else:
        request = model.format_request(
            messages,
            [],
            "Reply exactly as requested.",
        )
        configured_params = dict(model.config.get("params") or {})
        assert request.get("temperature") == configured_params.get("temperature")
        assert ("max_tokens" in request) != ("max_completion_tokens" in request)
        if "max_tokens" in configured_params:
            assert request["max_tokens"] == 128
        else:
            assert request["max_completion_tokens"] == 128
        if settings.get("reasoning_effort"):
            assert request["reasoning_effort"] == settings["reasoning_effort"]

    result = Agent(model=model, tools=[], system_prompt="Reply exactly as requested.")(
        "Reply exactly OK"
    )

    _assert_ok_text(result)


def test_live_litellm_runtime_model_switch_preflight_accepts_configured_model() -> None:
    settings = _runtime_settings()
    model = _litellm_model(stream=None, max_tokens=64, settings=settings)

    result = probe_strands_model(
        model,
        timeout_seconds=float(settings.get("request_timeout_seconds") or 120),
    )

    assert result["status"] == "passed"
    assert result["tools_exposed"] == 0


def test_live_litellm_current_config_model_accepts_strands_image_blocks() -> None:
    settings = _runtime_settings(prefer_instance_config=False)

    answer, reasoning = asyncio.run(_strands_image_block_answer(settings))

    assert "red" in answer.lower() or "red" in reasoning.lower()


@pytest.mark.parametrize("stream", [False, True], ids=["non_streaming", "streaming"])
def test_live_litellm_model_agent_turn_decision_structured_output_works(stream: bool) -> None:
    settings = _runtime_settings()
    if settings.get("litellm_api_mode") == "responses" and stream is False:
        pytest.skip("LiteLLM Responses requires streaming.")
    model = _litellm_model(stream=stream, max_tokens=800, settings=settings)
    agent = Agent(
        system_prompt=(
            "You are a runtime action planner. End this turn only through the "
            "structured-output tool requested by the caller."
        ),
        model=model,
        tools=[],
    )

    result = agent(
        "Return exactly READY as the user-visible answer in one task_response action.",
        structured_output_model=_turn_decision_output_model(settings),
        structured_output_prompt=_turn_decision_output_prompt(settings),
    )

    _assert_turn_decision_answer(result, "READY")


@pytest.mark.parametrize("stream", [False, True], ids=["non_streaming", "streaming"])
@fireworks_deepseek_smoke
def test_live_litellm_fireworks_deepseek_v4_flash_turn_decision_structured_output_works(stream: bool) -> None:
    settings = _runtime_settings(
        model_id_override=FIREWORKS_DEEPSEEK_V4_FLASH_MODEL_ID,
        prefer_instance_config=False,
    )
    model = _litellm_model(stream=stream, max_tokens=800, settings=settings)
    agent = Agent(
        system_prompt=(
            "You are a runtime action planner. End this turn only through the "
            "structured-output tool requested by the caller."
        ),
        model=model,
        tools=[],
    )

    assert _turn_decision_output_model(settings) is CompatibleModelAgentTurnDecision
    result = agent(
        "Return exactly FIREWORKS_READY as the user-visible answer in one task_response action.",
        structured_output_model=_turn_decision_output_model(settings),
        structured_output_prompt=_turn_decision_output_prompt(settings),
    )

    _assert_turn_decision_answer(result, "FIREWORKS_READY")


@fireworks_deepseek_smoke
def test_live_litellm_fireworks_deepseek_v4_flash_delegation_structured_output_works() -> None:
    settings = _runtime_settings(
        model_id_override=FIREWORKS_DEEPSEEK_V4_FLASH_MODEL_ID,
        prefer_instance_config=False,
    )
    model = _litellm_model(stream=False, max_tokens=800, settings=settings)
    agent = Agent(
        system_prompt=(
            "You are a runtime action planner. End this turn only through the "
            "structured-output tool requested by the caller."
        ),
        model=model,
        tools=[],
    )

    assert _turn_decision_output_model(settings) is CompatibleModelAgentTurnDecision
    result = agent(
        "Return one delegate_task action targeting ResearchComparisonAgent. "
        "Set task.problem_statement to: Compare the Volvo XC90 hybrid with its closest competitors. "
        "The delegated task must request a response.",
        structured_output_model=_turn_decision_output_model(settings),
        structured_output_prompt=_turn_decision_output_prompt(settings),
    )

    _assert_turn_decision_delegation(result, expected_target="ResearchComparisonAgent")


@fireworks_deepseek_smoke
def test_live_direct_fireworks_raw_model_accepts_runtime_settings_tool_schema() -> None:
    settings = _runtime_settings(
        model_id_override=FIREWORKS_RAW_DEEPSEEK_V4_FLASH_MODEL_ID,
        prefer_instance_config=False,
    )
    if "fireworks.ai" not in settings["litellm_api_base"].lower():
        pytest.skip("This direct-route regression test requires a Fireworks API base.")
    model = _litellm_model(stream=False, max_tokens=800, settings=settings)
    output_model = _turn_decision_output_model(settings)
    agent = Agent(
        system_prompt=(
            "You are a runtime action planner. Do not change runtime settings. End this turn only through "
            "the structured-output tool requested by the caller."
        ),
        model=model,
        tools=[plan_runtime_settings_change],
    )

    assert output_model is CompatibleModelAgentTurnDecision
    result = agent(
        "Return exactly FIREWORKS_RAW_READY as the user-visible answer in one task_response action.",
        structured_output_model=output_model,
        structured_output_prompt=_turn_decision_output_prompt(settings),
    )

    _assert_turn_decision_answer(result, "FIREWORKS_RAW_READY")


@fireworks_minimax_smoke
def test_live_litellm_fireworks_minimax_multi_tool_terminal_output_avoids_forced_retry() -> None:
    tool_calls: list[str] = []

    @tool
    def lookup_model_year(vehicle: str) -> str:
        """Return the model-year fact required by the live smoke task."""
        tool_calls.append("model_year")
        return f"{vehicle}: 2027 is a carryover refresh."

    @tool
    def lookup_ev_range(vehicle: str) -> str:
        """Return the electric-range fact required by the live smoke task."""
        tool_calls.append("ev_range")
        return f"{vehicle}: current electric range is 33 miles."

    settings = _runtime_settings(
        model_id_override=FIREWORKS_MINIMAX_M3_MODEL_ID,
        prefer_instance_config=False,
    )
    model = _litellm_model(stream=False, max_tokens=800, settings=settings)
    output_model = _turn_decision_output_model(settings)
    output_tool_name = output_model.__name__
    guardrails = RuntimeGuardrailsPlugin()
    agent = Agent(
        system_prompt=(
            "You are a research specialist. Use every lookup tool requested by the task, then end through the exact "
            "structured-output tool named in the terminal instruction."
        ),
        model=model,
        tools=[lookup_model_year, lookup_ev_range],
        hooks=[guardrails],
        callback_handler=None,
    )
    invocation_state = {
        RuntimeGuardrailsPlugin._TURN_DECISION_CONTEXT_KEY: {
            "agent_id": "live_minimax_smoke",
            "named_agent_type": "ResearchComparisonAgent",
            "task_id": "live_minimax_multi_tool",
            "provider": settings["model_provider"],
            "model_id": settings["model_id"],
            "expected_tool_name": output_tool_name,
        }
    }

    assert output_model is CompatibleModelAgentTurnDecision
    result = agent(
        "Call both lookup_model_year and lookup_ev_range for the Volvo XC90. After both results, return exactly "
        "MINIMAX_MULTI_TOOL_READY as the user-visible answer.\n\n"
        + llm_turn_runtime._turn_decision_initial_guidance(output_tool_name),
        invocation_state=invocation_state,
        structured_output_model=output_model,
        structured_output_prompt=_turn_decision_output_prompt(settings),
    )

    assert set(tool_calls) == {"model_year", "ev_range"}
    _assert_turn_decision_answer(result, "MINIMAX_MULTI_TOOL_READY")
    assert result.state.get(RuntimeGuardrailsPlugin._STRUCTURED_FORCE_COUNT_KEY, 0) == 0


def test_live_litellm_manager_prompt_with_small_tool_surface_works() -> None:
    model = _litellm_model(stream=None)
    tools = AGENT_TYPE_MAP["TeamArchitect"]["tools"][:3]
    agent = Agent(system_prompt=_manager_system_prompt(), model=model, tools=tools)

    result = agent(
        "Reply with exactly the text OK and nothing else.",
        structured_output_model=_turn_decision_output_model(),
        structured_output_prompt=_turn_decision_output_prompt(),
    )

    _assert_turn_decision_answer(result, "OK")


@pytest.mark.parametrize("stream", [True, False], ids=["streaming", "non_streaming"])
def test_live_litellm_manager_full_tool_surface_structured_output_call(stream: bool) -> None:
    settings = _runtime_settings()
    if settings.get("litellm_api_mode") == "responses" and stream is False:
        pytest.skip("LiteLLM Responses requires streaming.")
    model = _litellm_model(stream=stream, max_tokens=800, settings=settings)
    tools = AGENT_TYPE_MAP["TeamArchitect"]["tools"]
    agent = Agent(system_prompt=_manager_system_prompt(), model=model, tools=tools)

    result = agent(
        "Reply with exactly the text OK and nothing else.",
        structured_output_model=_turn_decision_output_model(settings),
        structured_output_prompt=_turn_decision_output_prompt(settings),
    )

    _assert_turn_decision_answer(result, "OK")
