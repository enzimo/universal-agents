from __future__ import annotations

from types import SimpleNamespace

import pytest
from strands.hooks import AfterModelCallEvent, AfterToolCallEvent


def test_runtime_guardrails_records_strands_forced_structured_output_retry(caplog):
    from custom_agents.runtime_guardrails import RuntimeGuardrailsPlugin

    monitor = RuntimeGuardrailsPlugin()
    invocation_state = {
        "invocation_state": {
            RuntimeGuardrailsPlugin._TURN_DECISION_CONTEXT_KEY: {
                "agent_id": "agent_1",
                "named_agent_type": "ResearchComparisonAgent",
                "task_id": "task_1",
                "correlation_id": "corr_1",
                "conversation_id": "conversation_1",
                "provider": "litellm",
                "model_id": "fireworks/minimax-m3",
                "expected_tool_name": "CompatibleModelAgentTurnDecision",
            }
        },
        "request_state": {},
    }
    event = AfterModelCallEvent(
        agent=SimpleNamespace(),
        invocation_state=invocation_state,
        stop_response=AfterModelCallEvent.ModelStopResponse(
            stop_reason="end_turn",
            message={
                "role": "assistant",
                "content": [
                    {"reasoningContent": {"reasoningText": {"text": "thinking"}}},
                    {"text": "draft answer"},
                ],
            },
        ),
    )

    with caplog.at_level("WARNING"):
        monitor.after_model_call(event)

    request_state = invocation_state["request_state"]
    assert request_state[RuntimeGuardrailsPlugin._STRUCTURED_FORCE_COUNT_KEY] == 1
    assert request_state[RuntimeGuardrailsPlugin._STRUCTURED_FORCE_LAST_KEY] == {
        "forced_retry_count": 1,
        "forced_retry_exhausted": False,
        "stop_reason": "end_turn",
        "expected_tool_name": "CompatibleModelAgentTurnDecision",
        "text_chars": 12,
        "reasoning_chars": 8,
        "tool_use_count": 0,
    }
    assert RuntimeGuardrailsPlugin._STRUCTURED_FALLBACK_KEY not in request_state
    assert "event=structured_output_forced_retry" in caplog.text
    assert "task_id=task_1" in caplog.text
    assert "expected_tool_name=CompatibleModelAgentTurnDecision" in caplog.text


def test_runtime_guardrails_plugin_warns_at_tool_call_thresholds():
    from custom_agents.runtime_guardrails import RuntimeGuardrailsPlugin

    monitor = RuntimeGuardrailsPlugin(max_tool_calls=4)
    invocation_state = {"request_state": {}}

    for index in range(2):
        event = AfterToolCallEvent(
            agent=SimpleNamespace(),
            selected_tool=SimpleNamespace(tool_name="crawl4ai_crawl"),
            tool_use={
                "name": "crawl4ai_crawl",
                "toolUseId": f"tool_{index}",
                "input": {},
            },
            invocation_state=invocation_state,
            result={
                "toolUseId": f"tool_{index}",
                "status": "success",
                "content": [{"text": "ok"}],
            },
        )
        monitor.after_tool_call(event)
        assert len(event.result["content"]) == 1

    event = AfterToolCallEvent(
        agent=SimpleNamespace(),
        selected_tool=SimpleNamespace(tool_name="crawl4ai_crawl"),
        tool_use={"name": "crawl4ai_crawl", "toolUseId": "tool_3", "input": {}},
        invocation_state=invocation_state,
        result={
            "toolUseId": "tool_3",
            "status": "success",
            "content": [{"text": "ok"}],
        },
    )
    monitor.after_tool_call(event)

    warning_text = event.result["content"][-1]["text"]
    assert "Tool call budget pressure is elevated" in warning_text
    assert "75% (3 / 4 (max) tool calls consumed)" in warning_text


@pytest.mark.parametrize(
    "tool_name",
    [
        "CompatibleModelAgentTurnDecision",
    ],
)
def test_runtime_guardrails_adds_repair_hint_for_empty_model_turn_decision_input(
    tool_name: str,
):
    from custom_agents.runtime_guardrails import RuntimeGuardrailsPlugin

    monitor = RuntimeGuardrailsPlugin(structured_output_failure_threshold=4)
    invocation_state = {"request_state": {}}

    event = AfterToolCallEvent(
        agent=SimpleNamespace(),
        selected_tool=SimpleNamespace(tool_name=tool_name),
        tool_use={
            "name": tool_name,
            "toolUseId": "structured_empty",
            "input": {},
        },
        invocation_state=invocation_state,
        result={
            "toolUseId": "structured_empty",
            "status": "error",
            "content": [{"text": "Field 'actions': Field required"}],
        },
    )

    monitor.after_tool_call(event)

    request_state = invocation_state["request_state"]
    assert request_state[RuntimeGuardrailsPlugin._STRUCTURED_COUNT_KEY] == 1
    assert "stop_event_loop" not in request_state
    repair_hint = event.result["content"][-1]["text"]
    assert "structured_output_repair_hint" in repair_hint
    assert tool_name in repair_hint
    assert "required non-empty actions list" in repair_hint
    assert "actions[0].task.answer" in repair_hint
    assert "wrote, updated, or saved a file" in repair_hint


def test_runtime_guardrails_keeps_legacy_structured_response_diagnostics():
    from custom_agents.runtime_guardrails import RuntimeGuardrailsPlugin

    monitor = RuntimeGuardrailsPlugin(structured_output_failure_threshold=1)
    invocation_state = {"request_state": {}}

    event = AfterToolCallEvent(
        agent=SimpleNamespace(),
        selected_tool=SimpleNamespace(tool_name="StructuredLLMResponse"),
        tool_use={
            "name": "StructuredLLMResponse",
            "toolUseId": "legacy_structured",
            "input": {
                "selected_message_type": "Task",
                "answer": "misnested",
            },
        },
        invocation_state=invocation_state,
        result={
            "toolUseId": "legacy_structured",
            "status": "error",
            "content": [{"text": "missing task"}],
        },
    )

    monitor.after_tool_call(event)

    diagnostics = invocation_state["request_state"][
        RuntimeGuardrailsPlugin._STRUCTURED_FALLBACK_KEY
    ]["last_input_diagnostics"]
    assert diagnostics["expected_payload_field"] == "task"
    assert diagnostics["has_expected_payload_field"] is False
    assert diagnostics["likely_misnested_task_fields"] == ["answer"]
