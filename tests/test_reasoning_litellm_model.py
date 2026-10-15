"""Tests for repo-owned LiteLLM reasoning-content compatibility."""

from __future__ import annotations

import json
import logging

import pytest

from custom_agents.reasoning_litellm_model import ReasoningAwareLiteLLMModel
from custom_agents.strands_model_factory import StrandsModelFactoryConfig, build_strands_model
from custom_tools.secure_cli_tool import secure_cli
from custom_tools.providers.runtime_settings import (
    plan_runtime_settings_change,
)


def _tool_turn_messages(
    model_trace: str = "reason about using the tool",
    tool_use_id: str = "call_weather",
) -> list[dict]:
    return [
        {
            "role": "assistant",
            "content": [
                {"reasoningContent": {"reasoningText": {"text": model_trace}}},
                {
                    "toolUse": {
                        "toolUseId": tool_use_id,
                        "name": "get_weather",
                        "input": {"location": "Dallas"},
                    }
                },
            ],
        },
        {
            "role": "user",
            "content": [{"text": "continue"}],
        },
    ]


def _tool_result_message(tool_use_id: str) -> dict:
    return {
        "role": "user",
        "content": [
            {
                "toolResult": {
                    "toolUseId": tool_use_id,
                    "status": "success",
                    "content": [{"text": "tool result"}],
                }
            }
        ],
    }


def _schema_nodes(value):
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from _schema_nodes(child)
    elif isinstance(value, list):
        for child in value:
            yield from _schema_nodes(child)


@pytest.mark.parametrize(
    ("model_id", "api_base"),
    [
        ("minimax/minimax-m3", "https://openrouter.ai/api/v1"),
        ("openrouter/minimax/minimax-m3", None),
    ],
)
def test_litellm_factory_uses_current_openrouter_token_parameter(
    model_id: str,
    api_base: str | None,
) -> None:
    model = build_strands_model(
        StrandsModelFactoryConfig(
            provider="litellm",
            model_id=model_id,
            litellm_api_base=api_base,
            max_output_tokens=321,
        )
    )

    assert model.config["params"]["max_completion_tokens"] == 321
    assert "max_tokens" not in model.config["params"]


def test_litellm_factory_keeps_legacy_token_parameter_for_generic_compatible_endpoint() -> None:
    model = build_strands_model(
        StrandsModelFactoryConfig(
            provider="litellm",
            model_id="custom-model",
            litellm_api_base="https://llm.example.test/v1",
            max_output_tokens=321,
        )
    )

    assert model.config["params"]["max_tokens"] == 321
    assert "max_completion_tokens" not in model.config["params"]


def test_unknown_litellm_provider_drops_reasoning_without_openai_warning(caplog) -> None:
    model = ReasoningAwareLiteLLMModel(model_id="openrouter/example-model")
    caplog.set_level(logging.WARNING)

    request = model.format_request(_tool_turn_messages())

    assistant_message = request["messages"][0]
    assert "reasoning_content" not in assistant_message
    assert assistant_message["content"] == ""
    assert assistant_message["tool_calls"][0]["id"] == "call_weather"
    assert "reasoningContent is not supported" not in caplog.text


@pytest.mark.parametrize(
    "model_id",
    [
        "fireworks/minimax-m3",
        "fireworks/minimax/minimax-m3",
        "litellm_proxy/fireworks/minimax/minimax-m3",
        "novita/minimax-m3",
        "novita/minimax/minimax-m3",
        "litellm_proxy/novita/minimax/minimax-m3",
    ],
)
def test_compatible_secure_cli_request_schema_advertises_nested_action_params(model_id: str) -> None:
    model = ReasoningAwareLiteLLMModel(model_id=model_id)

    request = model.format_request(
        [{"role": "user", "content": [{"text": "check Gmail with gog"}]}],
        tool_specs=[secure_cli.tool_spec],
    )

    schema = request["tools"][0]["function"]["parameters"]
    params_schema = schema["properties"]["params"]
    assert request["tools"][0]["function"]["name"] == "secure_cli"
    assert "credential_catalog" in schema["properties"]["provider"]["description"]
    assert "secure_requests" in schema["properties"]["provider"]["description"]
    assert "google" in schema["properties"]["provider"]["description"]
    assert "credential.get_metadata" in schema["properties"]["action"]["description"]
    assert "credential.bind_for_openshell" in schema["properties"]["action"]["description"]
    assert "upsert_openshell_binding" not in json.dumps(schema)
    assert "gmail_send" in schema["properties"]["action"]["description"]
    assert "gmail_search" in schema["properties"]["action"]["description"]
    assert "gog.exec" in schema["properties"]["action"]["description"]
    assert {
        "query",
        "limit",
        "argv",
        "json",
        "to",
        "subject",
        "body",
        "credential_key",
        "tool_id",
        "provider_type",
        "url",
        "method",
        "headers",
        "json_body",
        "timeout",
    }.issubset(params_schema["properties"])
    # The broker names the OpenShell sandbox from the verified person.
    assert "sandbox_name" not in params_schema["properties"]
    assert params_schema["properties"]["argv"]["type"] == "array"
    assert params_schema["properties"]["credential_key"]["type"] == "string"
    assert params_schema["properties"]["tool_id"]["type"] == "string"
    assert params_schema["properties"]["json_body"]["type"] == "object"
    assert params_schema["properties"]["json_body"]["additionalProperties"] is True
    # Requests carry no credential placeholders: the broker adds the linked account.
    assert "${" not in json.dumps(params_schema)
    for removed_binding_param in ("binding_name", "env", "allowed_hosts", "denied_hosts"):
        assert removed_binding_param not in params_schema["properties"]
    assert "params.to" in params_schema["description"]
    assert "params.subject" in params_schema["description"]
    assert "params.body" in params_schema["description"]
    assert "params.query" in params_schema["description"]
    assert "params.argv" in params_schema["description"]
    assert "params.credential_key" in params_schema["description"]
    assert "params.tool_id" in params_schema["description"]
    assert "params.binding_name" not in params_schema["description"]
    assert "params.url" in params_schema["description"]
    assert "params.json_body" in params_schema["description"]
    for action_param in (
        "query",
        "argv",
        "to",
        "subject",
        "body",
        "credential_key",
        "tool_id",
        "url",
        "json_body",
    ):
        assert action_param not in schema["properties"]

    unsupported_fireworks_keywords = {
        "oneOf",
        "pattern",
        "minLength",
        "maxLength",
        "minItems",
        "maxItems",
        "title",
        "default",
    }
    for node in _schema_nodes(schema):
        assert unsupported_fireworks_keywords.isdisjoint(node)

    public_schema = secure_cli.tool_spec["inputSchema"]["json"]
    assert public_schema["properties"]["params"] == {"description": "Parameter params", "type": "object"}


def test_direct_fireworks_api_base_rewrites_unprefixed_runtime_setting_schema() -> None:
    public_schema = plan_runtime_settings_change.tool_spec["inputSchema"]["json"]
    assert public_schema["$defs"]["RuntimeSettingChange"]["properties"]["value"] == {
        "title": "Value"
    }
    model = ReasoningAwareLiteLLMModel(
        client_args={"api_base": "https://api.fireworks.ai/inference/v1"},
        model_id="deepseek-v4-flash-0731",
    )

    request = model.format_request(
        [{"role": "user", "content": [{"text": "change runtime settings"}]}],
        tool_specs=[plan_runtime_settings_change.tool_spec],
    )

    schema = request["tools"][0]["function"]["parameters"]
    value_schema = schema["properties"]["changes"]["items"]["properties"]["value"]
    assert "$defs" not in schema
    assert value_schema["type"] == "string"
    assert "title" not in value_schema


def test_stale_deepseek_reasoning_tool_exchange_is_pruned_as_a_block() -> None:
    model = ReasoningAwareLiteLLMModel(
        model_id="parasail/deepseek-ai/DeepSeek-V4-Flash",
        reasoning_trace_retention_messages=3,
    )
    messages = [
        _tool_turn_messages("old trace", "call_old")[0],
        _tool_result_message("call_old"),
        {"role": "assistant", "content": [{"text": "Older tool result was handled."}]},
        {"role": "user", "content": [{"text": "one"}]},
        _tool_turn_messages("fresh trace", "call_fresh")[0],
        _tool_result_message("call_fresh"),
        {"role": "user", "content": [{"text": "current"}]},
    ]

    request = model.format_request(messages)

    rendered = str(request["messages"])
    assert "old trace" not in rendered
    assert "call_old" not in rendered
    assert "Older tool result was handled." in rendered
    assert request["messages"][2]["reasoning_content"] == "fresh trace"
    assert rendered.count("call_fresh") == 2


def test_reasoning_trace_retention_requires_positive_message_count() -> None:
    with pytest.raises(ValueError, match="reasoning_trace_retention_messages"):
        ReasoningAwareLiteLLMModel(
            model_id="parasail/deepseek-ai/DeepSeek-V4-Flash",
            reasoning_trace_retention_messages=0,
        )
