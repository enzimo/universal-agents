"""OpenAI Responses adapter that preserves reasoning items across tool turns.

Strands 1.50 normalizes OpenAI reasoning output into ``reasoningContent`` and
then drops that block when it formats the next Responses request. Stateless
reasoning calls must instead replay the provider's complete output items,
including encrypted reasoning content. This adapter is deliberately selected
only for OpenAI reasoning model ids on the public OpenAI, OpenAI Codex
subscription, and explicit LiteLLM Responses routes by
``strands_model_factory``.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncGenerator
from types import SimpleNamespace
from typing import Any

import openai

from custom_agents.openai_output_comparison import (
    difference_fields,
    encrypted_content_differences,
    material_output,
)
from strands.models._openai_errors import classify_openai_error
from strands.models.openai_responses import (
    OpenAIResponsesModel,
    _OpenAIResponsesStreamError,
)
from strands.types.content import Message, Messages
from strands.types.exceptions import (
    ContextWindowOverflowException,
    ModelThrottledException,
)
from strands.types.streaming import StreamEvent
from strands.types.tools import ToolChoice, ToolSpec

logger = logging.getLogger(__name__)

_MODEL_STATE_OUTPUT_BATCHES = "universal_agents_openai_reasoning_output_batches"
_ENCRYPTED_REASONING_INCLUDE = "reasoning.encrypted_content"


class OpenAIReasoningResponsesModel(OpenAIResponsesModel):
    """Replay complete OpenAI output items during a stateless Strands tool loop."""

    def __init__(
        self, client_args: dict[str, Any] | None = None, **model_config: Any
    ) -> None:
        params = dict(model_config.get("params") or {})
        raw_include = params.get("include") or []
        include = [raw_include] if isinstance(raw_include, str) else list(raw_include)
        if _ENCRYPTED_REASONING_INCLUDE not in include:
            include.append(_ENCRYPTED_REASONING_INCLUDE)
        params["include"] = include
        model_config["params"] = params
        super().__init__(client_args=client_args, **model_config)

    def _format_request(
        self,
        messages: Messages,
        tool_specs: list[ToolSpec] | None = None,
        system_prompt: str | None = None,
        tool_choice: ToolChoice | None = None,
        model_state: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        request = super()._format_request(
            [],
            tool_specs,
            system_prompt,
            tool_choice,
            model_state,
        )
        request["input"] = self._format_request_messages_with_state(
            messages, model_state
        )
        return request

    def _format_request_messages_with_state(
        self,
        messages: Messages,
        model_state: dict[str, Any] | None,
    ) -> list[dict[str, Any]]:
        batches_by_call_ids: dict[tuple[str, ...], list[dict[str, Any]]] = {}
        if model_state:
            for batch in model_state.get(_MODEL_STATE_OUTPUT_BATCHES, []):
                if not isinstance(batch, dict):
                    continue
                call_ids = tuple(str(value) for value in batch.get("call_ids", []))
                output = batch.get("output")
                if call_ids and isinstance(output, list):
                    batches_by_call_ids[call_ids] = output

        formatted: list[dict[str, Any]] = []
        for message in messages:
            contents = message.get("content", [])
            call_ids = tuple(
                str(content["toolUse"]["toolUseId"])
                for content in contents
                if "toolUse" in content
            )
            replay = (
                batches_by_call_ids.get(call_ids)
                if message.get("role") == "assistant"
                else None
            )
            if replay is not None:
                formatted.extend(replay)
                continue

            has_reasoning = any("reasoningContent" in content for content in contents)
            clean_message: Message = {
                **message,
                "content": [
                    content for content in contents if "reasoningContent" not in content
                ],
            }
            if has_reasoning:
                logger.warning(
                    "event=openai_reasoning_replay_missing model_id=%s tool_call_ids=%s "
                    "action=drop_unreplayable_normalized_reasoning",
                    self.config.get("model_id"),
                    list(call_ids),
                )
            formatted.extend(super()._format_request_messages([clean_message]))
        return formatted

    @staticmethod
    def _serialize_output_item(item: Any) -> dict[str, Any]:
        if isinstance(item, dict):
            return dict(item)
        model_dump = getattr(item, "model_dump", None)
        if not callable(model_dump):
            raise TypeError(
                f"Unsupported OpenAI response output item: {type(item).__name__}"
            )
        serialized = model_dump(mode="json", exclude_none=True)
        if not isinstance(serialized, dict):
            raise TypeError(
                "OpenAI response output item serialization returned "
                f"{type(serialized).__name__}, expected dict"
            )
        return serialized

    @classmethod
    def _serialize_output_items(cls, response: Any) -> list[dict[str, Any]]:
        return [
            cls._serialize_output_item(item)
            for item in getattr(response, "output", None) or []
        ]

    @staticmethod
    def _function_call_ids(output: list[dict[str, Any]]) -> tuple[str, ...]:
        return tuple(
            str(item["call_id"])
            for item in output
            if item.get("type") == "function_call" and item.get("call_id")
        )

    @staticmethod
    def _differing_output_indexes(
        streamed_output: list[dict[str, Any]],
        terminal_output: list[dict[str, Any]],
    ) -> list[int]:
        return [
            index
            for index in range(max(len(streamed_output), len(terminal_output)))
            if index >= len(streamed_output)
            or index >= len(terminal_output)
            or streamed_output[index] != terminal_output[index]
        ]

    @staticmethod
    def _ordered_completed_output_items(
        indexed_items: dict[int, dict[str, Any]],
        unindexed_items: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        return [item for _, item in sorted(indexed_items.items())] + list(
            unindexed_items
        )

    def _record_completed_output_item(
        self,
        event: Any,
        indexed_items: dict[int, dict[str, Any]],
        unindexed_items: list[dict[str, Any]],
    ) -> None:
        item = getattr(event, "item", None)
        if item is None:
            return
        serialized_item = self._serialize_output_item(item)
        output_index = getattr(event, "output_index", None)
        if isinstance(output_index, int) and output_index >= 0:
            if output_index in indexed_items:
                logger.warning(
                    "event=openai_reasoning_output_index_repeated "
                    "model_id=%s output_index=%s "
                    "action=prefer_latest_completed_item",
                    self.config.get("model_id"),
                    output_index,
                )
            indexed_items[output_index] = serialized_item
            return
        logger.warning(
            "event=openai_reasoning_output_index_missing "
            "model_id=%s action=append_after_indexed_items",
            self.config.get("model_id"),
        )
        unindexed_items.append(serialized_item)

    @staticmethod
    def _remember_output_batch(
        model_state: dict[str, Any] | None,
        response: Any,
        streamed_output: list[dict[str, Any]] | None = None,
    ) -> None:
        if model_state is None:
            return
        try:
            terminal_output = OpenAIReasoningResponsesModel._serialize_output_items(
                response
            )
            completed_stream_output = list(streamed_output or [])
            streamed_call_ids = OpenAIReasoningResponsesModel._function_call_ids(
                completed_stream_output
            )
            terminal_call_ids = OpenAIReasoningResponsesModel._function_call_ids(
                terminal_output
            )
            if not streamed_call_ids and not terminal_call_ids:
                return
            if (
                completed_stream_output
                and terminal_output
                and completed_stream_output != terminal_output
            ):
                differing_indexes = (
                    OpenAIReasoningResponsesModel._differing_output_indexes(
                        completed_stream_output,
                        terminal_output,
                    )
                )
                material_stream_output = material_output(completed_stream_output)
                material_terminal_output = material_output(terminal_output)
                log = (
                    logger.warning
                    if material_stream_output != material_terminal_output
                    else logger.debug
                )
                log(
                    "event=openai_reasoning_output_sources_mismatch model_id=%s "
                    "streamed_item_count=%s terminal_item_count=%s "
                    "differing_output_indexes=%s material=%s "
                    "action=%s response_id=%s differing_fields=%s "
                    "encrypted_content_diagnostics=%s",
                    getattr(response, "model", None),
                    len(completed_stream_output),
                    len(terminal_output),
                    differing_indexes,
                    material_stream_output != material_terminal_output,
                    "prefer_completed_stream_items"
                    if streamed_call_ids
                    else "prefer_terminal_items",
                    getattr(response, "id", None),
                    difference_fields(completed_stream_output, terminal_output),
                    encrypted_content_differences(
                        completed_stream_output, terminal_output
                    ),
                )
            # The Codex subscription backend emits complete output items through
            # response.output_item.done while leaving response.completed.output
            # empty. Public OpenAI currently supplies both representations.
            # Prefer the stream only when it contains the function calls needed
            # for the current Strands tool continuation. A partial stream must
            # not hide a replayable terminal batch.
            output = completed_stream_output if streamed_call_ids else terminal_output
            call_ids = OpenAIReasoningResponsesModel._function_call_ids(output)
            if not call_ids:
                return
            batches = list(model_state.get(_MODEL_STATE_OUTPUT_BATCHES, []))
            batches = [
                batch
                for batch in batches
                if tuple(batch.get("call_ids", [])) != call_ids
            ]
            batches.append({"call_ids": list(call_ids), "output": output})
            # The owning UniversalAgent clears model_state at the top-level task
            # boundary, so retaining every batch here preserves the complete
            # inner tool loop without creating cross-task growth.
            model_state[_MODEL_STATE_OUTPUT_BATCHES] = batches
        except Exception as exc:
            logger.warning(
                "event=openai_reasoning_output_capture_failed error_type=%s error=%s",
                type(exc).__name__,
                exc,
            )

    async def stream(
        self,
        messages: Messages,
        tool_specs: list[ToolSpec] | None = None,
        system_prompt: str | None = None,
        *,
        tool_choice: ToolChoice | None = None,
        model_state: dict[str, Any] | None = None,
        **kwargs: Any,
    ) -> AsyncGenerator[StreamEvent, None]:
        """Stream a response and retain its raw output items for the next tool turn."""

        request = self._format_request(
            messages, tool_specs, system_prompt, tool_choice, model_state
        )
        async with openai.AsyncOpenAI(**self._resolve_client_args()) as client:
            try:
                response = await client.responses.create(**request)
                yield self._format_chunk({"chunk_type": "message_start"})

                tool_calls: dict[str, dict[str, str]] = {}
                completed_output_items_by_index: dict[int, dict[str, Any]] = {}
                unindexed_completed_output_items: list[dict[str, Any]] = []
                final_usage = None
                data_type: str | None = None
                stop_reason: str | None = None

                async for event in response:
                    event_type = getattr(event, "type", None)
                    if event_type == "response.created":
                        response_id = getattr(
                            getattr(event, "response", None), "id", None
                        )
                        if model_state is not None and response_id:
                            model_state["response_id"] = response_id
                    elif event_type in (
                        "response.reasoning_text.delta",
                        "response.reasoning_summary_text.delta",
                    ):
                        chunks, data_type = self._stream_switch_content(
                            "reasoning_content", data_type
                        )
                        for chunk in chunks:
                            yield chunk
                        delta = getattr(event, "delta", None)
                        if isinstance(delta, str):
                            yield self._format_chunk(
                                {
                                    "chunk_type": "content_delta",
                                    "data_type": "reasoning_content",
                                    "data": delta,
                                }
                            )
                    elif event_type == "response.output_text.delta":
                        chunks, data_type = self._stream_switch_content(
                            "text", data_type
                        )
                        for chunk in chunks:
                            yield chunk
                        delta = getattr(event, "delta", None)
                        if isinstance(delta, str):
                            yield self._format_chunk(
                                {
                                    "chunk_type": "content_delta",
                                    "data_type": "text",
                                    "data": delta,
                                }
                            )
                    elif event_type == "response.output_text.annotation.added":
                        annotation = getattr(event, "annotation", None)
                        if annotation and annotation.get("type") == "url_citation":
                            yield self._format_chunk(
                                {
                                    "chunk_type": "content_delta",
                                    "data_type": "citation",
                                    "data": annotation,
                                }
                            )
                    elif event_type == "response.output_item.added":
                        item = getattr(event, "item", None)
                        if getattr(item, "type", None) == "function_call":
                            call_id = str(getattr(item, "call_id", "unknown"))
                            tool_calls[call_id] = {
                                "name": str(getattr(item, "name", "")),
                                "arguments": "",
                                "call_id": call_id,
                                "item_id": str(getattr(item, "id", "")),
                            }
                    elif event_type == "response.output_item.done":
                        self._record_completed_output_item(
                            event,
                            completed_output_items_by_index,
                            unindexed_completed_output_items,
                        )
                    elif event_type == "response.function_call_arguments.delta":
                        item_id = getattr(event, "item_id", None)
                        for call_info in tool_calls.values():
                            if call_info["item_id"] == item_id:
                                call_info["arguments"] += str(
                                    getattr(event, "delta", "")
                                )
                                break
                    elif event_type == "response.function_call_arguments.done":
                        item_id = getattr(event, "item_id", None)
                        for call_info in tool_calls.values():
                            if call_info["item_id"] == item_id:
                                call_info["arguments"] = str(
                                    getattr(event, "arguments", "")
                                )
                                break
                    elif event_type == "response.failed":
                        error = getattr(getattr(event, "response", None), "error", None)
                        raise _OpenAIResponsesStreamError(
                            getattr(error, "message", None),
                            getattr(error, "code", None),
                        )
                    elif event_type == "error":
                        raise _OpenAIResponsesStreamError(
                            getattr(event, "message", None),
                            getattr(event, "code", None),
                        )
                    elif event_type == "response.incomplete":
                        terminal_response = getattr(event, "response", None)
                        final_usage = getattr(terminal_response, "usage", None)
                        completed_output_items = self._ordered_completed_output_items(
                            completed_output_items_by_index,
                            unindexed_completed_output_items,
                        )
                        self._remember_output_batch(
                            model_state,
                            terminal_response,
                            completed_output_items,
                        )
                        incomplete_details = getattr(
                            terminal_response, "incomplete_details", None
                        )
                        if (
                            getattr(incomplete_details, "reason", None)
                            == "max_output_tokens"
                        ):
                            stop_reason = "length"
                        break
                    elif event_type == "response.completed":
                        terminal_response = getattr(event, "response", None)
                        final_usage = getattr(terminal_response, "usage", None)
                        completed_output_items = self._ordered_completed_output_items(
                            completed_output_items_by_index,
                            unindexed_completed_output_items,
                        )
                        self._remember_output_batch(
                            model_state,
                            terminal_response,
                            completed_output_items,
                        )
                        break
            except (openai.APIError, _OpenAIResponsesStreamError) as error:
                error_kind = classify_openai_error(error)
                if error_kind == "throttling":
                    raise ModelThrottledException(str(error)) from error
                if error_kind == "context_overflow":
                    raise ContextWindowOverflowException(str(error)) from error
                raise

            if data_type:
                yield self._format_chunk(
                    {"chunk_type": "content_stop", "data_type": data_type}
                )
            for call_info in tool_calls.values():
                tool_call = SimpleNamespace(
                    function=SimpleNamespace(
                        name=call_info["name"],
                        arguments=call_info["arguments"],
                    ),
                    id=call_info["call_id"],
                )
                yield self._format_chunk(
                    {
                        "chunk_type": "content_start",
                        "data_type": "tool",
                        "data": tool_call,
                    }
                )
                yield self._format_chunk(
                    {
                        "chunk_type": "content_delta",
                        "data_type": "tool",
                        "data": tool_call,
                    }
                )
                yield self._format_chunk(
                    {"chunk_type": "content_stop", "data_type": "tool"}
                )

            finish_reason = (
                "tool_calls"
                if tool_calls
                else "length"
                if stop_reason == "length"
                else "stop"
            )
            yield self._format_chunk(
                {"chunk_type": "message_stop", "data": finish_reason}
            )
            if final_usage:
                yield self._format_chunk(
                    {"chunk_type": "metadata", "data": final_usage}
                )
