from __future__ import annotations

from custom_agents.task_operation_runtime import TaskOperationRuntime
from tests.llm_turn_fixtures import turn_executor_for

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from strands.models.litellm import LiteLLMModel
from strands.models.openai import OpenAIModel

from custom_agents import model_failover_runtime
from custom_agents.provider_error_runtime import (
    ProviderTimeoutError,
)
from custom_agents.reasoning_summary_recovery import ReasoningSummaryRepair
from custom_messages.contracts.attachments_v1 import (
    AttachmentStorageRef,
    UserTaskAttachment,
)
from custom_messages.contracts.delivery_v1 import UserNotification
from custom_messages.contracts.routing_v1 import (
    GatewayContext,
    MessageHeader,
    MessageMeta,
)
from custom_messages.contracts.task_v1 import Task
from custom_messages.enums import MessageMetaType


def _quiet_logger() -> SimpleNamespace:
    return SimpleNamespace(
        info=lambda *_, **__: None,
        warning=lambda *_, **__: None,
    )


@pytest.mark.asyncio
async def test_terminal_provider_timeout_unwinds_without_duplicate_traceback(caplog):
    import logging

    clear = MagicMock()
    async def prepare(turn):
        turn.context_request = None
        turn.assembly = None

    subject = SimpleNamespace(
        id="timeout_agent",
        _logger=logging.getLogger("timeout_turn_test"),
        _prepare_turn=prepare,
        _prepare_invocation=AsyncMock(),
        _invoke_model=AsyncMock(side_effect=ProviderTimeoutError()),
        _write_context_debug_post_turn=AsyncMock(),
        _clear_active_execution_context=clear,
    )
    executor = turn_executor_for(subject)
    with pytest.raises(ProviderTimeoutError, match="model service timed out"):
        await executor.process_llm_interaction(
            Task(task_id="timeout", task_type=MessageMetaType.REQUEST, problem_statement="work"),
            MessageHeader(), MessageMeta(),
        )
    clear.assert_called_once()
    subject._write_context_debug_post_turn.assert_awaited_once()
    assert not caplog.records


def _image_attachment(
    path, *, content_type: str = "image/png", kind: str = "image"
) -> UserTaskAttachment:
    data = path.read_bytes()
    return UserTaskAttachment(
        attachment_id="att_image_1",
        filename=path.name,
        content_type=content_type,
        kind=kind,
        size_bytes=len(data),
        sha256="sha",
        storage_refs=[AttachmentStorageRef(storage_type="local", path=str(path))],
    )


def test_terminal_reasoning_model_diagnostics_reports_safe_params() -> None:
    class FakeModel:
        def get_config(self):
            return {
                "model_id": "litellm_proxy/novita/minimax/minimax-m3",
                "params": {
                    "stream": True,
                    "reasoning_effort": "medium",
                    "api_key": "secret-value",
                },
            }

    agent = SimpleNamespace(_strands_agent=SimpleNamespace(model=FakeModel()))

    diagnostics = turn_executor_for(agent)._terminal_reasoning_model_diagnostics()

    assert diagnostics["model_adapter"] == "FakeModel"
    assert (
        diagnostics["model_config_model_id"]
        == "litellm_proxy/novita/minimax/minimax-m3"
    )
    assert diagnostics["model_config_stream"] is True
    assert diagnostics["model_config_reasoning_effort"] == "medium"
    assert diagnostics["model_config_params"]["api_key"] == "[redacted]"


def test_reasoning_summary_repair_notification_is_gateway_visible(tmp_path) -> None:
    send_message = AsyncMock()
    agent = SimpleNamespace(
        id="agent_1",
        name="Assistant",
        team_comms=SimpleNamespace(send_message=send_message),
        _logger=_quiet_logger(),
    )
    repair = ReasoningSummaryRepair(
        provider="litellm",
        model_id="openai/gpt-5.6-luna",
        requested_summary="detailed",
        override_path=tmp_path / "LLM_OVERRIDES.yaml",
        yaml_path=("model_capabilities.litellm.openai/gpt-5.6-luna.reasoning_summary"),
        backup_path=tmp_path / "LLM_OVERRIDES.yaml.bak",
        changed=True,
    )

    notified = asyncio.run(
        turn_executor_for(agent)._notify_user_of_reasoning_summary_repair(
            repair=repair,
            task_id="task_1",
            msg_header=MessageHeader(response_routing_key="gateway.reply"),
            msg_meta=MessageMeta(
                conversation_id="conversation_1",
                gateway_context=GatewayContext(session_key="session_1"),
            ),
        )
    )

    assert notified is True
    envelope = send_message.await_args.kwargs["message"]
    assert envelope.msg_content.reason == "reasoning_summary_unsupported_repaired"
    assert (
        "ensure model_capabilities.litellm.openai/gpt-5.6-luna.reasoning_summary is null"
        in (envelope.msg_content.text)
    )
    assert send_message.await_args.kwargs["routing_key"] == "gateway.reply"


def _failover_outcome(
    *,
    retry_inline: bool = True,
    selection_reason: str = "provider_failover",
) -> model_failover_runtime.ModelFailoverOutcome:
    return model_failover_runtime.ModelFailoverOutcome(
        from_candidate=model_failover_runtime.ModelCandidate(
            provider="litellm",
            model_id="openai/primary-model",
        ),
        to_candidate=model_failover_runtime.ModelCandidate(
            provider="openai",
            model_id="fallback-model",
        ),
        notice="Switched model provider.",
        retry_inline=retry_inline,
        requeue_required=not retry_inline,
        selection_reason=selection_reason,
    )


def test_model_failover_notification_is_gateway_system_message() -> None:
    send_message = AsyncMock()
    agent = SimpleNamespace(
        id="agent_1",
        name="Assistant",
        llm_failover={"enabled": True, "notify_user": True},
        instance_config_path=None,
        team_comms=SimpleNamespace(send_message=send_message),
        _logger=_quiet_logger(),
    )
    gateway_context = GatewayContext(
        session_key="session_1", delivery_mode="interactive"
    )
    task = Task(
        task_id="task_1",
        task_type=MessageMetaType.REQUEST,
        problem_statement="Finish the request.",
    )

    notified = asyncio.run(
        turn_executor_for(agent)._notify_user_of_model_failover(
            outcome=_failover_outcome(),
            task_id=task.task_id,
            msg_content=task,
            msg_header=MessageHeader(response_routing_key="gateway.reply"),
            msg_meta=MessageMeta(
                msg_correlation_id="task_correlation",
                conversation_id="conversation_1",
                gateway_context=gateway_context,
            ),
        )
    )

    assert notified is True
    envelope = send_message.await_args.kwargs["message"]
    assert isinstance(envelope.msg_content, UserNotification)
    assert envelope.msg_content.reason == "model_provider_failover"
    assert "litellm/openai/primary-model" in envelope.msg_content.text
    assert "openai/fallback-model" in envelope.msg_content.text
    assert "is retrying the request" in envelope.msg_content.text
    assert envelope.msg_meta.msg_correlation_id != "task_correlation"
    assert envelope.msg_meta.msg_correlation_id == envelope.msg_content.notification_id
    assert envelope.msg_header.idempotency_key == envelope.msg_content.notification_id
    assert envelope.msg_meta.gateway_context is not gateway_context
    assert send_message.await_args.kwargs["routing_key"] == "gateway.reply"


@pytest.mark.asyncio
async def test_provider_context_retry_is_suppressed_after_tool_activity() -> None:
    error = RuntimeError("maximum context length exceeded")
    task_manager = SimpleNamespace(
        operations_runtime=TaskOperationRuntime(), append_history_entry=MagicMock()
    )
    agent = SimpleNamespace(
        id="agent_1",
        name="Agent",
        inner_loop_context={"reactive_overflow": {"max_total_recovery_attempts": 3}},
        _strands_agent=SimpleNamespace(messages=[]),
        _logger=_quiet_logger(),
        task_manager=task_manager,
        _should_recover_context_overflow=lambda: True,
        _is_provider_context_overflow_error=lambda candidate: candidate is error,
        _max_provider_context_retry_attempts=lambda: 1,
    )
    tool_call_limit_handler = SimpleNamespace(tool_sequence=[{"tool": "mutating_tool"}])
    invocation_state: dict[str, object] = {}

    with pytest.raises(RuntimeError) as raised:
        await turn_executor_for(agent)._resolve_provider_context_retry(
            task_id="task_1",
            error=error,
            invocation_state=invocation_state,
            provider_retry_attempt=0,
            tool_call_limit_handler=tool_call_limit_handler,
        )

    assert raised.value is error
    assert invocation_state == {}
    details = task_manager.append_history_entry.call_args.kwargs["details"]
    assert details["suppression_reason"] == "tool_activity_started"


def test_build_strands_prompt_input_includes_image_blocks_when_supported(
    tmp_path,
) -> None:
    image_path = tmp_path / "mystery.png"
    image_path.write_bytes(b"\x89PNG\r\n\x1a\nimage-bytes")
    task = Task(
        task_type=MessageMetaType.REQUEST,
        problem_statement="Can you see this?",
        attachments=[_image_attachment(image_path)],
    )
    agent = SimpleNamespace(
        id="agent_vision", supports_image_input=True, _logger=_quiet_logger()
    )

    prompt = turn_executor_for(agent)._build_strands_prompt_input("full prompt", task)

    assert isinstance(prompt, list)
    assert prompt[0]["text"].startswith("full prompt")
    assert "native image content blocks" in prompt[0]["text"]
    assert prompt[1]["text"].startswith("Direct image attachment:")
    assert prompt[2]["image"]["format"] == "png"
    assert prompt[2]["image"]["source"]["bytes"] == image_path.read_bytes()


def test_litellm_formatter_preserves_direct_image_block_as_image_url() -> None:
    formatted = LiteLLMModel.format_request_messages(
        [
            {
                "role": "user",
                "content": [
                    {"text": "describe this image"},
                    {"image": {"format": "png", "source": {"bytes": b"abc"}}},
                ],
            }
        ]
    )

    assert formatted == [
        {
            "role": "user",
            "content": [
                {"text": "describe this image", "type": "text"},
                {
                    "image_url": {
                        "detail": "auto",
                        "format": "image/png",
                        "url": "data:image/png;base64,YWJj",
                    },
                    "type": "image_url",
                },
            ],
        }
    ]


def test_openai_formatter_preserves_direct_image_block_as_image_url() -> None:
    formatted = OpenAIModel.format_request_messages(
        [
            {
                "role": "user",
                "content": [
                    {"text": "describe this image"},
                    {"image": {"format": "png", "source": {"bytes": b"abc"}}},
                ],
            }
        ]
    )

    assert formatted == [
        {
            "role": "user",
            "content": [
                {"text": "describe this image", "type": "text"},
                {
                    "image_url": {
                        "detail": "auto",
                        "format": "image/png",
                        "url": "data:image/png;base64,YWJj",
                    },
                    "type": "image_url",
                },
            ],
        }
    ]
