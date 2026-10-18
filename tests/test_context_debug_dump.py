from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from custom_agents.context_debug_dump import (
    ContextDebugDumpPlugin,
    ContextDebugDumpWriter,
    register_context_debug_model_observer,
)
from custom_agents.context_engine import AssembledContext, ContextAssemblyRequest, ContextSection
from custom_messages.contracts.routing_v1 import MessageHeader, MessageMeta
from custom_messages.contracts.task_v1 import Task
from custom_messages.enums import MessageMetaType
from runtime_settings.instance_layout import InstanceLayout


def _dump_dir(tmp_path: Path) -> Path:
    """Context dumps are diagnostics in the instance's state/diagnostics."""
    return InstanceLayout.at(tmp_path).diagnostics_dir / "context_dumps"


def _request() -> ContextAssemblyRequest:
    task = Task(
        task_id="task_debug_1",
        task_type=MessageMetaType.REQUEST,
        problem_statement="Inspect this prompt.",
    )
    return ContextAssemblyRequest(
        incoming_content=task,
        execution_task_id=task.task_id,
        incoming_header=MessageHeader(from_agent_id="agent_user", agent_signature=""),
        incoming_meta=MessageMeta(
            from_agent_name="Taylor",
            msg_correlation_id="corr_debug_1",
            conversation_id="conversation_debug_1",
            request_timezone="America/Chicago",
        ),
        identity_meta=MessageMeta(
            from_agent_name="Taylor",
            msg_correlation_id="corr_debug_1",
            conversation_id="conversation_debug_1",
            request_timezone="America/Chicago",
        ),
        incoming_message_json='{"message_class":"CommsMessage","api_key":"value"}',
    )


def _assembly() -> AssembledContext:
    return AssembledContext(
        sections=[
            ContextSection(
                name="current_request",
                text="Current Request:\nInspect this prompt.",
                priority=100,
                pinned=True,
                source="test",
                char_count=37,
                estimated_tokens=10,
                plugin_id="coremem",
                scope_kind="team_id",
                degradable=False,
            )
        ],
        prompt="System context\n\nCurrent Request:\nInspect this prompt.\napi_key: should-not-survive",
        char_count=78,
        estimated_tokens=20,
        source_lengths=[
            {
                "name": "current_request",
                "tokens": 10,
                "chars": 37,
                "plugin_id": "coremem",
                "scope_kind": "team_id",
                "degradable": False,
            }
        ],
        warnings=[{"message": "near budget"}],
        max_context_tokens=100,
        soft_limit_tokens=75,
        hard_limit_tokens=90,
        reserved_output_tokens=10,
        context_fill_ratio=0.8,
    )


def test_context_debug_dump_writes_prompt_sections_and_metadata(tmp_path: Path) -> None:
    writer = ContextDebugDumpWriter(
        config={
            "enabled": True,
            "mode": "always",
            "include_incoming_envelope_json": True,
        },
        output_dir=_dump_dir(tmp_path),
    )

    dump_dir = writer.dump_pre_invocation(
        request=_request(),
        assembly=_assembly(),
        agent_metadata={"agent_id": "agent_debug", "system_prompt": "System prompt"},
    )

    assert dump_dir is not None
    assert dump_dir.is_dir()
    assert (dump_dir / "pre_metadata.json").exists()
    pre_metadata = json.loads((dump_dir / "pre_metadata.json").read_text(encoding="utf-8"))
    assert pre_metadata["context"]["estimated_tokens"] == 20
    assert pre_metadata["context"]["max_context_tokens"] == 100
    assert "tool specifications" in pre_metadata["context"]["excluded_from_estimate"]
    assert "system_prompt" not in pre_metadata["agent"]
    assert pre_metadata["agent"]["system_prompt_chars"] == len("System prompt")
    assert pre_metadata["agent"]["system_prompt_artifact"] == "system_prompt.md"
    assert (dump_dir / "system_prompt.md").read_text(encoding="utf-8").strip() == "System prompt"
    assert "api_key=[REDACTED]" in (dump_dir / "prompt.md").read_text(encoding="utf-8")
    section = json.loads((dump_dir / "sections.json").read_text(encoding="utf-8"))[0]
    assert section["name"] == "current_request"
    assert section["plugin_id"] == "coremem"
    assert section["scope_kind"] == "team_id"
    assert section["estimated_tokens"] == 10
    source = json.loads((dump_dir / "source_lengths.json").read_text(encoding="utf-8"))[0]
    assert source["name"] == "current_request"
    assert source["plugin_id"] == "coremem"
    assert source["tokens"] == 10
    incoming_payload = json.loads((dump_dir / "incoming_envelope.json").read_text(encoding="utf-8"))
    assert incoming_payload["message_class"] == "CommsMessage"
    assert incoming_payload["api_key"] == "[REDACTED]"


def test_context_debug_dump_post_turn_redacts_secrets_and_can_omit_tool_results(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setenv("TEST_API_KEY", "super-secret-value")
    writer = ContextDebugDumpWriter(
        config={
            "enabled": True,
            "mode": "on_error",
            "include_tool_results": False,
        },
        output_dir=_dump_dir(tmp_path),
    )

    dump_dir = writer.dump_post_turn(
        request=_request(),
        assembly=_assembly(),
        agent_metadata={"agent_id": "agent_debug", "system_prompt": "System prompt"},
        result={"api_key": "abc123", "note": "token: super-secret-value"},
        parsed_content=Task(
            task_id="task_debug_1",
            task_type=MessageMetaType.RESPONSE,
            answer="Done",
        ),
        reasoning_artifacts={
            "note": "observable only; not hidden chain-of-thought",
            "context_warnings": [{"message": "near budget"}],
            "tool_trace": [{"event": "tool_use", "tool_name": "search", "input_preview": "token: super-secret-value"}],
        },
        strands_messages=[
            {
                "role": "user",
                "content": [{"toolResult": {"content": [{"text": "large secret payload"}]}}],
            }
        ],
        error=RuntimeError("boom"),
    )

    assert dump_dir is not None
    result_payload = json.loads((dump_dir / "result.json").read_text(encoding="utf-8"))
    assert result_payload["api_key"] == "[REDACTED]"
    assert "super-secret-value" not in result_payload["note"]

    strands_messages = json.loads((dump_dir / "strands_messages.json").read_text(encoding="utf-8"))
    assert strands_messages[0]["content"][0]["toolResult"] == {
        "omitted": "tool results excluded by debug.context_dump.include_tool_results=false"
    }
    reasoning_artifacts = json.loads((dump_dir / "reasoning_artifacts.json").read_text(encoding="utf-8"))
    assert reasoning_artifacts["tool_trace"][0]["tool_name"] == "search"
    assert "super-secret-value" not in reasoning_artifacts["tool_trace"][0]["input_preview"]
    assert json.loads((dump_dir / "error.json").read_text(encoding="utf-8"))["type"] == "RuntimeError"


def test_context_debug_dump_redacts_binary_bytes_in_strands_messages(tmp_path: Path) -> None:
    writer = ContextDebugDumpWriter(
        config={
            "enabled": True,
            "mode": "always",
            "include_tool_results": True,
        },
        output_dir=_dump_dir(tmp_path),
    )

    dump_dir = writer.dump_post_turn(
        request=_request(),
        assembly=_assembly(),
        agent_metadata={"agent_id": "agent_debug", "system_prompt": "System prompt"},
        strands_messages=[
            {
                "role": "user",
                "content": [{"image": {"format": "png", "source": {"bytes": b"binary-image"}}}],
            }
        ],
    )

    assert dump_dir is not None
    strands_messages = json.loads((dump_dir / "strands_messages.json").read_text(encoding="utf-8"))
    bytes_payload = strands_messages[0]["content"][0]["image"]["source"]["bytes"]
    assert bytes_payload["omitted_binary_bytes"] == len(b"binary-image")
    assert "sha256" in bytes_payload


def test_context_debug_dump_writes_sanitized_strands_prompt_input(tmp_path: Path) -> None:
    writer = ContextDebugDumpWriter(
        config={
            "enabled": True,
            "mode": "always",
        },
        output_dir=_dump_dir(tmp_path),
    )

    dump_dir = writer.dump_strands_prompt_input(
        request=_request(),
        assembly=_assembly(),
        agent_metadata={"agent_id": "agent_debug", "system_prompt": "System prompt"},
        strands_prompt_input=[
            {"text": "Describe this image.\napi_key: should-not-survive"},
            {"image": {"format": "png", "source": {"bytes": b"binary-image"}}},
        ],
        attempt=0,
    )

    assert dump_dir is not None
    latest = json.loads((dump_dir / "strands_prompt_input_latest.json").read_text(encoding="utf-8"))
    assert latest["summary"]["image_block_count"] == 1
    assert latest["summary"]["blocks"][1]["bytes"] == len(b"binary-image")
    assert latest["input"][0]["text"]["omitted_text_chars"] == len("Describe this image.\napi_key: should-not-survive")
    assert latest["input"][0]["text"]["preview"] == "Describe this image.\napi_key=[REDACTED]"
    bytes_payload = latest["input"][1]["image"]["source"]["bytes"]
    assert bytes_payload["omitted_binary_bytes"] == len(b"binary-image")
    assert "sha256" in bytes_payload


def test_context_debug_redaction_preserves_token_accounting_and_redacts_secret_tokens(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("LLM_CONTEXT_WINDOW_TOKENS", "220000")
    monkeypatch.setenv("TEST_ACCESS_TOKEN", "access-token-secret")
    writer = ContextDebugDumpWriter(
        config={"enabled": True, "mode": "always"},
        output_dir=_dump_dir(tmp_path),
    )

    redacted = writer._redact(
        {
            "estimated_tokens": 1200,
            "inputTokens": 900,
            "max_context_tokens": 220000,
            "token_count": 42,
            "access_token": "access-token-secret",
            "access_token_file": "/run/secrets/access-token",
            "api_key": "secret-key",
            "credentials_dir": "/safe/diagnostic/path",
        }
    )
    rendered = writer._redact_string(
        "estimated_tokens: 1200 inputTokens: 900 access_token: access-token-secret context=220000"
    )

    assert redacted["estimated_tokens"] == 1200
    assert redacted["inputTokens"] == 900
    assert redacted["max_context_tokens"] == 220000
    assert redacted["token_count"] == 42
    assert redacted["access_token"] == "[REDACTED]"
    assert redacted["access_token_file"] == "/run/secrets/access-token"
    assert redacted["api_key"] == "[REDACTED]"
    assert redacted["credentials_dir"] == "/safe/diagnostic/path"
    assert "estimated_tokens: 1200" in rendered
    assert "inputTokens: 900" in rendered
    assert "access-token-secret" not in rendered
    assert "context=220000" in rendered


@pytest.mark.asyncio
@pytest.mark.parametrize("nested", [False, True], ids=["flat", "nested_like_a_real_turn"])
async def test_model_call_observer_captures_normal_and_forced_calls_with_optional_details(
    tmp_path: Path, nested: bool
) -> None:
    from strands._middleware.registry import MiddlewareRegistry
    from strands._middleware.stages import InvokeModelContext, InvokeModelStage

    plugin = ContextDebugDumpPlugin(
        config={
            "enabled": True,
            "mode": "always",
            "capture_model_calls": True,
            "include_model_call_inputs": True,
            "include_tool_specs": True,
        },
        output_dir=_dump_dir(tmp_path),
        agent_metadata_provider=lambda: {"agent_id": "agent_debug", "system_prompt": "Base prompt"},
    )
    plugin.assemble(_request(), _assembly())
    fake_agent = SimpleNamespace(_middleware_registry=MiddlewareRegistry())
    fake_model = SimpleNamespace(model_id="test-model")
    assert register_context_debug_model_observer(fake_agent, plugin) is True

    invocation_state = {
        ContextDebugDumpPlugin.INVOCATION_CONTEXT_KEY: {
            "task_id": "task_debug_1",
            "correlation_id": "corr_debug_1",
            "provider": "test",
            "model_id": "test-model",
        }
    }
    if nested:
        # A real turn passes callback_handler as an extra keyword, so Strands
        # nests the turn's invocation_state (runtime_context.caller_invocation_state).
        invocation_state = {"callback_handler": print, "invocation_state": invocation_state}

    async def invoke(context: InvokeModelContext, usage: dict[str, int]) -> None:
        async def terminal(_context):
            yield {"delta": "ok"}
            yield {
                "stop": (
                    "end_turn",
                    {"role": "assistant", "content": [{"text": "done"}]},
                    usage,
                    {"latencyMs": 12},
                )
            }

        async for _event in fake_agent._middleware_registry.invoke(
            InvokeModelStage,
            context,
            terminal,
        ):
            pass

    await invoke(
        InvokeModelContext(
            agent=fake_agent,
            messages=[{"role": "user", "content": [{"text": "api_key: hidden-value"}]}],
            system_prompt="Base system prompt",
            tool_specs=[
                {
                    "name": "search",
                    "description": "Search docs",
                    "inputSchema": {
                        "type": "object",
                        "properties": {"api_key": {"type": "string"}},
                    },
                },
                {"name": "read", "description": "Read docs", "inputSchema": {"type": "object"}},
            ],
            tool_choice={"auto": {}},
            invocation_state=invocation_state,
            model=fake_model,
            projected_input_tokens=101,
        ),
        {"inputTokens": 100, "outputTokens": 20, "totalTokens": 120},
    )
    await invoke(
        InvokeModelContext(
            agent=fake_agent,
            messages=[{"role": "user", "content": [{"text": "Finalize"}]}],
            system_prompt="Base system prompt",
            tool_specs=[
                {
                    "name": "ModelAgentTurnDecision",
                    "description": "Return terminal actions",
                    "inputSchema": {"type": "object", "properties": {"actions": {"type": "array"}}},
                }
            ],
            tool_choice={"tool": {"name": "ModelAgentTurnDecision"}},
            invocation_state=invocation_state,
            model=fake_model,
            projected_input_tokens=55,
        ),
        {"inputTokens": 54, "outputTokens": 10, "totalTokens": 64},
    )

    plugin.post_invocation(_request(), _assembly(), result={"status": "ok"})

    dump_dir = next((_dump_dir(tmp_path) / "task_debug_1").iterdir())
    model_calls = json.loads((dump_dir / "model_calls.json").read_text(encoding="utf-8"))
    assert model_calls["call_count"] == 2
    assert model_calls["usage_totals"] == {
        "inputTokens": 154,
        "outputTokens": 30,
        "totalTokens": 184,
    }
    assert model_calls["calls"][0]["input_accounting"]["projected_input_tokens"] == 101
    assert model_calls["calls"][0]["input_accounting"]["tool_specs"]["tool_count"] == 2
    assert model_calls["calls"][1]["input_accounting"]["tool_specs"]["tool_count"] == 1
    detail = json.loads((dump_dir / "model_calls" / "call_001.json").read_text(encoding="utf-8"))
    assert detail["captured_inputs"]["messages"][0]["content"][0]["text"] == "api_key=[REDACTED]"
    assert len(detail["captured_inputs"]["tool_specs"]) == 2
    assert detail["captured_inputs"]["tool_specs"][0]["inputSchema"]["properties"]["api_key"] == {
        "type": "string"
    }


def test_model_call_observer_registration_is_opt_in(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    plugin = ContextDebugDumpPlugin(
        config={"enabled": True, "capture_model_calls": False},
        output_dir=_dump_dir(tmp_path),
        agent_metadata_provider=dict,
    )

    with caplog.at_level("WARNING"):
        registered = register_context_debug_model_observer(SimpleNamespace(), plugin)

    assert registered is False
    assert plugin.model_call_observer_status == {
        "registered": False,
        "api": "strands._middleware.InvokeModelStage.Wrap",
        "reason": "capture_model_calls_disabled",
    }
    assert "continuing without it" not in caplog.text
