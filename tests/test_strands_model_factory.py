from __future__ import annotations

import asyncio
import logging
from types import SimpleNamespace

import pytest

from tests.model_key_support import env_model_keys
from custom_agents.filtered_tool_result_context import (
    FilteredToolResultConversationManager,
)
from custom_agents.tool_result_offloader import TaskAwareWorkspaceStorage
from custom_agents.reasoning_litellm_model import ReasoningAwareLiteLLMModel
from custom_agents.openai_reasoning_responses_model import OpenAIReasoningResponsesModel
from custom_agents.strands_model_factory import (
    StrandsModelFactoryConfig,
    build_strands_model,
)
from strands import Agent
from strands.models.openai import OpenAIModel
from strands.models.openai_responses import OpenAIResponsesModel


_PROBE_TOOL = {
    "name": "connection_probe",
    "description": "Return a connection status.",
    "inputSchema": {
        "json": {
            "type": "object",
            "properties": {},
            "additionalProperties": False,
        }
    },
}
_PROBE_MESSAGES = [
    {
        "role": "user",
        "content": [{"text": "Check the connection."}],
    }
]


def test_native_openai_uses_stateless_responses_request_for_reasoning_tools(
    caplog,
) -> None:
    model = build_strands_model(
        StrandsModelFactoryConfig(
            provider="openai",
            model_id="gpt-5.6-luna",
            temperature=0.0,
            reasoning_effort="medium",
            reasoning_summary="auto",
            max_output_tokens=1_024,
            model_keys=env_model_keys("test-key", env_var="OPENAI_API_KEY"),
        )
    )

    assert isinstance(model, OpenAIResponsesModel)
    assert isinstance(model, OpenAIReasoningResponsesModel)
    assert model.stateful is False
    request = model._format_request(  # noqa: SLF001 - verifies the provider wire contract.
        _PROBE_MESSAGES,
        [_PROBE_TOOL],
        "Use tools when useful.",
        model_state={"response_id": "resp_previous"},
    )

    assert request["model"] == "gpt-5.6-luna"
    assert request["reasoning"] == {"effort": "medium", "summary": "auto"}
    assert request["include"] == ["reasoning.encrypted_content"]
    assert "temperature" not in request
    assert request["max_output_tokens"] == 1_024
    assert request["store"] is False
    assert "previous_response_id" not in request
    assert request["tools"] == [
        {
            "type": "function",
            "name": "connection_probe",
            "description": "Return a connection status.",
            "parameters": {
                "type": "object",
                "properties": {},
                "additionalProperties": False,
            },
        }
    ]
    assert "messages" not in request
    assert "reasoning_effort" not in request
    assert "max_completion_tokens" not in request
    assert (
        "event=model_parameter_omitted provider=openai model_id=gpt-5.6-luna "
        "parameter=temperature reason=unsupported_openai_reasoning_model"
    ) in caplog.text


def test_native_openai_keeps_temperature_for_non_reasoning_model() -> None:
    model = build_strands_model(
        StrandsModelFactoryConfig(
            provider="openai",
            model_id="gpt-4o",
            temperature=0.25,
            model_keys=env_model_keys("test-key", env_var="OPENAI_API_KEY"),
        )
    )

    request = model._format_request(  # noqa: SLF001 - verifies the provider wire contract.
        _PROBE_MESSAGES,
        [],
        model_state={},
    )

    assert request["temperature"] == 0.25
    assert type(model) is OpenAIResponsesModel
    assert "include" not in request


def test_openai_compatible_fallback_passes_api_key_as_client_configuration() -> None:
    model = build_strands_model(
        StrandsModelFactoryConfig(
            provider="openrouter",
            model_id="openai/gpt-4o-mini",
            model_keys=env_model_keys("test-compatible-key", env_var="OPENAI_API_KEY"),
        )
    )

    assert isinstance(model, OpenAIModel)
    # The key is looked up for each request, never held by the model.
    assert asyncio.run(model.client_args["api_key"]()) == "test-compatible-key"
    assert "api_key" not in model.config


def test_native_openai_keeps_temperature_for_gpt_5_image_model() -> None:
    model = build_strands_model(
        StrandsModelFactoryConfig(
            provider="openai",
            model_id="gpt-5-image",
            temperature=0.25,
            model_keys=env_model_keys("test-key", env_var="OPENAI_API_KEY"),
        )
    )

    assert model.config["params"]["temperature"] == 0.25


def test_native_openai_stateless_tool_continuation_preserves_call_id() -> None:
    model = build_strands_model(
        StrandsModelFactoryConfig(
            provider="openai",
            model_id="gpt-5.6-luna",
            reasoning_effort="medium",
            model_keys=env_model_keys("test-key", env_var="OPENAI_API_KEY"),
        )
    )
    messages = [
        {"role": "user", "content": [{"text": "Check the connection."}]},
        {
            "role": "assistant",
            "content": [
                {
                    "toolUse": {
                        "toolUseId": "call_123",
                        "name": "connection_probe",
                        "input": {},
                    }
                }
            ],
        },
        {
            "role": "user",
            "content": [
                {
                    "toolResult": {
                        "toolUseId": "call_123",
                        "status": "success",
                        "content": [{"text": "ready"}],
                    }
                }
            ],
        },
    ]

    request = model._format_request(  # noqa: SLF001 - verifies the provider wire contract.
        messages,
        [],
        "Use tools when useful.",
        model_state={"response_id": "resp_ignored"},
    )

    assert request["reasoning"] == {"effort": "medium"}
    assert request["store"] is False
    assert "previous_response_id" not in request
    assert request["input"][1] == {
        "type": "function_call",
        "call_id": "call_123",
        "name": "connection_probe",
        "arguments": "{}",
    }
    assert request["input"][2] == {
        "type": "function_call_output",
        "call_id": "call_123",
        "output": "ready",
    }


def test_native_openai_stateless_tool_continuation_replays_raw_reasoning_items(
    caplog,
) -> None:
    model = build_strands_model(
        StrandsModelFactoryConfig(
            provider="openai",
            model_id="gpt-5.6-luna",
            reasoning_effort="medium",
            model_keys=env_model_keys("test-key", env_var="OPENAI_API_KEY"),
        )
    )
    model_state: dict[str, object] = {}
    raw_output = [
        {
            "id": "rs_123",
            "type": "reasoning",
            "summary": [{"type": "summary_text", "text": "Checked the connection."}],
            "encrypted_content": "encrypted-reasoning-payload",
        },
        {
            "id": "fc_123",
            "type": "function_call",
            "call_id": "call_123",
            "name": "connection_probe",
            "arguments": "{}",
            "status": "completed",
        },
    ]
    OpenAIReasoningResponsesModel._remember_output_batch(  # noqa: SLF001
        model_state,
        SimpleNamespace(output=raw_output),
    )
    messages = [
        {"role": "user", "content": [{"text": "Check the connection."}]},
        {
            "role": "assistant",
            "content": [
                {"reasoningContent": {"text": "Checked the connection."}},
                {
                    "toolUse": {
                        "toolUseId": "call_123",
                        "name": "connection_probe",
                        "input": {},
                    }
                },
            ],
        },
        {
            "role": "user",
            "content": [
                {
                    "toolResult": {
                        "toolUseId": "call_123",
                        "status": "success",
                        "content": [{"text": "ready"}],
                    }
                }
            ],
        },
    ]

    request = model._format_request(messages, [], model_state=model_state)  # noqa: SLF001

    assert request["input"][1:3] == raw_output
    assert request["input"][3] == {
        "type": "function_call_output",
        "call_id": "call_123",
        "output": "ready",
    }
    assert "reasoningContent is not yet supported" not in caplog.text
    assert "event=openai_reasoning_replay_missing" not in caplog.text


def test_reasoning_output_capture_skips_non_tool_source_mismatches(caplog) -> None:
    model_state: dict[str, object] = {}
    streamed_output = [
        {
            "id": "rs_123",
            "type": "reasoning",
            "encrypted_content": "encrypted-reasoning-payload",
        },
        {
            "id": "msg_123",
            "type": "message",
            "status": "completed",
            "role": "assistant",
            "content": [{"type": "output_text", "text": "ready"}],
        },
    ]
    terminal_output = [
        streamed_output[0],
        {**streamed_output[1], "status": "in_progress"},
    ]

    OpenAIReasoningResponsesModel._remember_output_batch(  # noqa: SLF001
        model_state,
        SimpleNamespace(model="gpt-5.6-luna", output=terminal_output),
        streamed_output,
    )

    assert model_state == {}
    assert "event=openai_reasoning_output_sources_mismatch" not in caplog.text


def test_reasoning_output_items_are_ordered_by_provider_index() -> None:
    reasoning_item = {"id": "rs_123", "type": "reasoning"}
    function_item = {
        "id": "fc_123",
        "type": "function_call",
        "call_id": "call_123",
    }

    assert OpenAIReasoningResponsesModel._ordered_completed_output_items(  # noqa: SLF001
        {1: function_item, 0: reasoning_item},
        [],
    ) == [reasoning_item, function_item]


def test_responses_stream_captures_completed_output_items_when_terminal_output_is_empty(
    monkeypatch,
    caplog,
) -> None:
    model = build_strands_model(
        StrandsModelFactoryConfig(
            provider="openai",
            model_id="gpt-5.6-luna",
            reasoning_summary="auto",
            model_keys=env_model_keys("test-key", env_var="OPENAI_API_KEY"),
        )
    )
    raw_output = [
        {
            "id": "rs_123",
            "type": "reasoning",
            "summary": [{"type": "summary_text", "text": "Use the probe."}],
            "encrypted_content": "encrypted-reasoning-payload",
        },
        {
            "id": "fc_123",
            "type": "function_call",
            "call_id": "call_123",
            "name": "connection_probe",
            "arguments": "{}",
            "status": "completed",
        },
    ]
    events = [
        SimpleNamespace(
            type="response.output_item.added",
            item=SimpleNamespace(
                type="function_call",
                call_id="call_123",
                name="connection_probe",
                id="fc_123",
            ),
        ),
        SimpleNamespace(
            type="response.function_call_arguments.done",
            item_id="fc_123",
            arguments="{}",
        ),
        SimpleNamespace(
            type="response.output_item.done",
            output_index=0,
            item=raw_output[0],
        ),
        SimpleNamespace(type="response.output_item.done", item=raw_output[1]),
        SimpleNamespace(
            type="response.completed",
            response=SimpleNamespace(
                model="gpt-5.6-luna",
                output=[],
                usage=None,
            ),
        ),
    ]
    captured_request: dict[str, object] = {}

    class FakeStream:
        def __aiter__(self):
            self._events = iter(events)
            return self

        async def __anext__(self):
            try:
                return next(self._events)
            except StopIteration as exc:
                raise StopAsyncIteration from exc

    class FakeResponses:
        async def create(self, **request):
            captured_request.update(request)
            return FakeStream()

    class FakeClient:
        responses = FakeResponses()

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

    monkeypatch.setattr(
        "custom_agents.openai_reasoning_responses_model.openai.AsyncOpenAI",
        lambda **_kwargs: FakeClient(),
    )
    model_state: dict[str, object] = {}

    async def collect_events():
        return [
            event
            async for event in model.stream(
                _PROBE_MESSAGES,
                [_PROBE_TOOL],
                model_state=model_state,
            )
        ]

    with caplog.at_level(
        logging.WARNING,
        logger="custom_agents.openai_reasoning_responses_model",
    ):
        normalized_events = asyncio.run(collect_events())

    assert captured_request["include"] == ["reasoning.encrypted_content"]
    assert model_state["universal_agents_openai_reasoning_output_batches"] == [
        {"call_ids": ["call_123"], "output": raw_output}
    ]
    assert "event=openai_reasoning_output_index_missing" in caplog.text
    assert normalized_events[-1] == {"messageStop": {"stopReason": "tool_use"}}


def test_native_openai_model_accepts_universal_agents_conversation_manager(
    tmp_path,
) -> None:
    model = build_strands_model(
        StrandsModelFactoryConfig(
            provider="openai",
            model_id="gpt-5.6-luna",
            reasoning_effort="medium",
            model_keys=env_model_keys("test-key", env_var="OPENAI_API_KEY"),
        )
    )
    conversation_manager = FilteredToolResultConversationManager(
        storage=TaskAwareWorkspaceStorage(artifact_root=tmp_path),
        model=model,
    )

    agent = Agent(
        model=model,
        tools=[],
        conversation_manager=conversation_manager,
    )

    assert agent.model is model
    assert agent.conversation_manager is conversation_manager


def test_native_openai_stateless_consumers_format_tool_requests_without_optional_params() -> (
    None
):
    model = build_strands_model(
        StrandsModelFactoryConfig(
            provider="openai",
            model_id="gpt-5.6-luna",
            model_keys=env_model_keys("test-key", env_var="OPENAI_API_KEY"),
        )
    )

    assert isinstance(model, OpenAIResponsesModel)
    assert model.stateful is False
    assert model.config["params"] == {"include": ["reasoning.encrypted_content"]}
    request = model._format_request(  # noqa: SLF001 - verifies the provider wire contract.
        _PROBE_MESSAGES,
        [_PROBE_TOOL],
        model_state={"response_id": "resp_ignored"},
    )

    assert request["store"] is False
    assert "previous_response_id" not in request
    assert request["tools"][0]["name"] == "connection_probe"


def test_openrouter_litellm_route_filters_unsupported_openai_temperature(
    caplog,
) -> None:
    model = build_strands_model(
        StrandsModelFactoryConfig(
            provider="litellm",
            model_id="openai/gpt-5.6-luna",
            temperature=0.0,
            reasoning_effort="medium",
            max_output_tokens=1_024,
            litellm_api_base="https://openrouter.ai/api/v1",
            model_keys=env_model_keys("test-key"),
        )
    )

    assert isinstance(model, ReasoningAwareLiteLLMModel)
    assert model.config["params"] == {
        "max_completion_tokens": 1_024,
        "reasoning_effort": "medium",
    }
    request = model.format_request(
        _PROBE_MESSAGES, [_PROBE_TOOL], "Use tools when useful."
    )
    assert request["model"] == "litellm_proxy/openai/gpt-5.6-luna"
    assert request["max_completion_tokens"] == 1_024
    assert request["reasoning_effort"] == "medium"
    assert "temperature" not in request
    assert request["tools"][0]["function"]["name"] == "connection_probe"
    assert (
        "event=model_parameter_omitted provider=litellm model_id=openai/gpt-5.6-luna "
        "parameter=temperature reason=unsupported_openai_reasoning_model"
    ) in caplog.text


def test_litellm_responses_route_uses_openai_responses_adapter_and_reasoning_events() -> (
    None
):
    model = build_strands_model(
        StrandsModelFactoryConfig(
            provider="litellm",
            model_id="openai/gpt-5.6-luna",
            reasoning_effort="xhigh",
            reasoning_summary="auto",
            max_output_tokens=1_024,
            litellm_api_mode="responses",
            litellm_api_base="https://proxy.example.test/v1",
            model_keys=env_model_keys("test-key"),
            request_timeout_seconds=45,
        )
    )

    assert isinstance(model, OpenAIResponsesModel)
    assert isinstance(model, OpenAIReasoningResponsesModel)
    assert model.stateful is False
    assert model.client_args["base_url"] == "https://proxy.example.test/v1"
    assert model.client_args["timeout"] == 45.0
    # The key is looked up for each request (custom_agents.model_keys), never held by the model.
    assert asyncio.run(model.client_args["api_key"]()) == "test-key"
    request = model._format_request(  # noqa: SLF001 - verifies the provider wire contract.
        _PROBE_MESSAGES,
        [_PROBE_TOOL],
        model_state={},
    )
    assert request["model"] == "openai/gpt-5.6-luna"
    assert request["reasoning"] == {"effort": "xhigh", "summary": "auto"}
    assert request["include"] == ["reasoning.encrypted_content"]
    assert request["max_output_tokens"] == 1_024
    assert request["store"] is False
    assert model._format_chunk(  # noqa: SLF001 - verifies normalized callback events.
        {
            "chunk_type": "content_delta",
            "data_type": "reasoning_content",
            "data": "Checked the relevant constraints.",
        }
    ) == {
        "contentBlockDelta": {
            "delta": {"reasoningContent": {"text": "Checked the relevant constraints."}}
        }
    }


def test_reasoning_summary_rejects_chat_completions_transport() -> None:
    with pytest.raises(
        ValueError, match="reasoning_summary requires the OpenAI Responses API"
    ):
        build_strands_model(
            StrandsModelFactoryConfig(
                provider="litellm",
                model_id="openai/gpt-5.6-luna",
                reasoning_summary="auto",
                litellm_api_base="https://proxy.example.test/v1",
            )
        )


def test_litellm_responses_route_rejects_disabled_streaming() -> None:
    with pytest.raises(ValueError, match="requires streaming"):
        build_strands_model(
            StrandsModelFactoryConfig(
                provider="litellm",
                model_id="openai/gpt-5.6-luna",
                litellm_api_mode="responses",
                stream=False,
            )
        )


def test_generic_openai_compatible_litellm_route_keeps_legacy_token_parameter() -> None:
    model = build_strands_model(
        StrandsModelFactoryConfig(
            provider="litellm",
            model_id="custom/demo-model",
            temperature=0.25,
            reasoning_effort="medium",
            max_output_tokens=1_024,
            litellm_api_base="https://compatible.example.test/v1",
            model_keys=env_model_keys("test-key"),
        )
    )

    assert isinstance(model, ReasoningAwareLiteLLMModel)
    assert model.config["params"] == {
        "temperature": 0.25,
        "max_tokens": 1_024,
        "reasoning_effort": "medium",
    }
    request = model.format_request(
        _PROBE_MESSAGES, [_PROBE_TOOL], "Use tools when useful."
    )
    assert request["temperature"] == 0.25
    assert request["max_tokens"] == 1_024
    assert request["reasoning_effort"] == "medium"
    assert request["tools"][0]["function"]["name"] == "connection_probe"


def test_generic_openai_compatible_route_filters_bare_openai_reasoning_model() -> None:
    model = build_strands_model(
        StrandsModelFactoryConfig(
            provider="litellm",
            model_id="gpt-5.6-luna",
            temperature=0.0,
            reasoning_effort="medium",
            max_output_tokens=1_024,
            litellm_api_base="https://compatible.example.test/v1",
            model_keys=env_model_keys("test-key"),
        )
    )

    assert isinstance(model, ReasoningAwareLiteLLMModel)
    assert model.config["params"] == {
        "max_completion_tokens": 1_024,
        "reasoning_effort": "medium",
    }
    request = model.format_request(
        _PROBE_MESSAGES, [_PROBE_TOOL], "Use tools when useful."
    )
    assert request["max_completion_tokens"] == 1_024
    assert request["reasoning_effort"] == "medium"
    assert "max_tokens" not in request
    assert "temperature" not in request
    assert request["tools"][0]["function"]["name"] == "connection_probe"


def test_litellm_chat_route_treats_later_gpt_generations_as_reasoning_models() -> None:
    """A newer GPT generation must not fall back to the legacy max_tokens contract.

    LiteLLM 1.100.0 forwarded max_tokens unchanged for gpt-6 models and OpenAI
    rejected it. LiteLLM's translation can lag new generations, so the runtime
    sends max_completion_tokens itself.
    """
    model = build_strands_model(
        StrandsModelFactoryConfig(
            provider="litellm",
            model_id="openai/gpt-6-luna",
            temperature=0.2,
            max_output_tokens=1_024,
            litellm_api_base="https://proxy.example.test/v1",
            model_keys=env_model_keys("test-key"),
        )
    )

    assert isinstance(model, ReasoningAwareLiteLLMModel)
    assert model.config["params"] == {"max_completion_tokens": 1_024}
    request = model.format_request(
        _PROBE_MESSAGES, [_PROBE_TOOL], "Use tools when useful."
    )
    assert request["max_completion_tokens"] == 1_024
    assert "max_tokens" not in request
    assert "temperature" not in request


# Well-known providers: one fixed address each, the key looked up per call ---------------------


def test_well_known_providers_send_their_key_only_to_their_fixed_address(monkeypatch) -> None:
    from custom_agents.model_keys import _BINDING_MARKER, ResolvedKey, _CALL_KEYS
    from custom_messages.contracts.model_keys_v1 import KeySource

    monkeypatch.setenv("OPENAI_BASE_URL", "https://elsewhere.example.com/v1")
    monkeypatch.setenv("ANTHROPIC_BASE_URL", "https://elsewhere.example.com")

    def build(provider: str, env_var: str):
        return build_strands_model(
            StrandsModelFactoryConfig(provider=provider, model_id="m", model_keys=env_model_keys(f"{provider}-key", env_var=env_var))
        )

    openai_model = build("openai", "OPENAI_API_KEY")
    xai_model = build("xai", "XAI_API_KEY")
    anthropic_model = build("anthropic", "ANTHROPIC_API_KEY")
    gemini_model = build("gemini", "GEMINI_API_KEY")

    assert openai_model.client_args["base_url"] == "https://api.openai.com/v1"
    assert asyncio.run(openai_model.client_args["api_key"]()) == "openai-key"
    assert xai_model.client_args["base_url"] == "https://api.x.ai/v1"
    assert asyncio.run(xai_model.client_args["api_key"]()) == "xai-key"
    assert str(anthropic_model.client.base_url).rstrip("/") == "https://api.anthropic.com"
    _CALL_KEYS.set({"https://api.anthropic.com": ResolvedKey("https://api.anthropic.com", "sk-ant-call", KeySource.STORED)})
    assert anthropic_model.client.auth_headers["X-Api-Key"] == "sk-ant-call"
    _CALL_KEYS.set({"https://generativelanguage.googleapis.com": ResolvedKey("https://generativelanguage.googleapis.com", "AIza-call", KeySource.STORED)})
    assert dict(gemini_model.client_args)["api_key"] == "AIza-call"
    assert all(getattr(model.stream, _BINDING_MARKER, False) or getattr(model.stream, "__wrapped__", None)
               for model in (openai_model, xai_model, anthropic_model, gemini_model))


def test_an_unknown_model_without_a_window_gets_the_fallback_with_one_warning(caplog) -> None:
    from custom_agents import strands_model_factory as factory

    factory._warned_context_window_fallbacks.discard(("litellm", "claude-haiku-5-5"))
    with caplog.at_level("WARNING", logger=factory.__name__):
        first = factory.resolve_context_window_tokens(provider="litellm", model_id="claude-haiku-5-5")
        again = factory.resolve_context_window_tokens(provider="litellm", model_id="claude-haiku-5-5")

    assert first == again == factory.FALLBACK_CONTEXT_WINDOW_TOKENS
    warnings = [record for record in caplog.records if "event=context_window_fallback" in record.getMessage()]
    assert len(warnings) == 1
    assert "model_id=claude-haiku-5-5" in warnings[0].getMessage()


def test_a_set_or_known_window_needs_no_guess(caplog) -> None:
    from custom_agents import strands_model_factory as factory

    with caplog.at_level("WARNING", logger=factory.__name__):
        explicit = factory.resolve_context_window_tokens(
            provider="litellm", model_id="claude-haiku-5-5", explicit_context_window_tokens=1_000_000
        )
        named = factory.resolve_context_window_tokens(provider="litellm", model_id="gpt-3.5-turbo")
        xai = factory.resolve_context_window_tokens(provider="xai", model_id="grok-anything")

    assert explicit == 1_000_000
    assert named == 16385
    # The agent and the model object share this rule; the agent's old copy gave xai 128K.
    assert xai == factory.XAI_OAUTH_CONTEXT_WINDOW_TOKENS
    assert not [record for record in caplog.records if "context_window_fallback" in record.getMessage()]
