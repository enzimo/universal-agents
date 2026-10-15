"""LiteLLM model adapter with provider-specific reasoning replay support."""

from __future__ import annotations

import copy
import logging
import re
from collections.abc import Mapping
from typing import Any, cast

from default_values import LLM_DEFAULTS
from strands.models.litellm import LiteLLMModel
from strands.types.content import ContentBlock, Messages, SystemContentBlock
from strands.types.tools import ToolChoice, ToolSpec

from custom_agents.litellm_compatibility import uses_litellm_compatible_schema

logger = logging.getLogger(__name__)

SECURE_CLI_COMPAT_PARAMS_DESCRIPTION = (
    "Action-specific secure broker parameters. Put every provider/action "
    "argument inside this params object, never as a top-level secure_cli "
    "argument. Examples: credential_catalog.credential.get_metadata uses "
    "params.credential_key; credential.bind_for_openshell uses params.tool_id "
    "and optional params.provider_type; "
    "secure_requests uses params.url plus optional params.headers or "
    "params.json_body, with no credentials: the broker adds the account "
    "linked to the web API tool; google.gmail_search uses params.query; google.gog.exec "
    "uses params.argv; google.gmail_send uses params.to, params.subject, and "
    "params.body. Never put these values in routing metadata, msg_meta, or any "
    "top-level tool argument."
)

_RUNTIME_SETTINGS_SCHEMA_HINT_TOOL_NAMES = {
    "plan_runtime_setting_change",
    "plan_runtime_settings_change",
}
_RUNTIME_SETTING_VALUE_DESCRIPTION = (
    "Required setting value encoded as text. Use true or false for booleans, "
    "a decimal representation for numbers, and the literal setting value for strings."
)

try:  # pragma: no cover - private helper location is SDK-version dependent.
    from strands.models._validation import _has_location_source
except Exception:  # pragma: no cover
    def _has_location_source(content: ContentBlock) -> bool:
        """Fallback for Strands SDK versions that removed the private helper."""

        if not isinstance(content, dict):
            return False
        for block_key in ("document", "image", "video"):
            block = content.get(block_key)
            if not isinstance(block, dict):
                continue
            source = block.get("source")
            if isinstance(source, dict) and "location" in source:
                return True
        return False


class ReasoningAwareLiteLLMModel(LiteLLMModel):
    """LiteLLM adapter that preserves reasoning traces when providers require it.

    Strands normalizes provider thinking output into ``reasoningContent`` blocks.
    The base OpenAI-compatible formatter drops those blocks because most Chat
    Completions endpoints do not accept them as ordinary content. DeepSeek
    thinking-mode tool calls are the important exception: assistant messages
    that contain tool calls must replay the generated ``reasoning_content`` in
    subsequent requests so the provider can continue the same reasoning turn.
    """

    def __init__(
        self,
        client_args: Mapping[str, Any] | None = None,
        *,
        reasoning_trace_retention_messages: int | None = LLM_DEFAULTS.reasoning_trace_retention_messages.value,
        **model_config: Any,
    ) -> None:
        context_window_limit = model_config.pop("context_window_limit", None)
        self.reasoning_trace_retention_messages = self._normalize_reasoning_trace_retention_messages(
            reasoning_trace_retention_messages
        )
        self._litellm_api_base = str(
            (client_args or {}).get("api_base") or (client_args or {}).get("base_url") or ""
        ).strip()
        model_config = self._with_provider_defaults(model_config)
        # A mapping, not a dict, when its key is looked up per call
        # (custom_agents.model_keys.CallKeyedClientArgs); LiteLLM only reads it.
        super().__init__(client_args=cast(dict[str, Any] | None, client_args), **model_config)
        if context_window_limit is not None:
            resolved_context_window_limit = int(context_window_limit)
            self.config["context_window_limit"] = resolved_context_window_limit

    @classmethod
    def _with_provider_defaults(cls, model_config: dict[str, Any]) -> dict[str, Any]:
        model_id = str(model_config.get("model_id", "")).lower()
        if "deepseek" not in model_id:
            return model_config
        params = dict(model_config.get("params") or {})
        if cls._is_deepseek_v4_streaming_blocked(model_id):
            params["stream"] = False
        elif "stream" not in params:
            params["stream"] = False
        return {**model_config, "params": params}

    @staticmethod
    def _is_deepseek_v4_streaming_blocked(model_id: str) -> bool:
        return re.search(r"deepseek.*v4.*(?:flash|pro)", model_id) is not None

    @staticmethod
    def _normalize_reasoning_trace_retention_messages(value: int | None) -> int | None:
        if value is None:
            return None
        normalized = int(value)
        if normalized < 1:
            raise ValueError("reasoning_trace_retention_messages must be >= 1, or None to disable pruning.")
        return normalized

    @property
    def _reasoning_replay_mode(self) -> str:
        model_id = str(self.config.get("model_id", "")).lower()
        if "deepseek" in model_id:
            return "deepseek_tool_calls"
        return "none"

    @property
    def _uses_provider_tool_schema_hints(self) -> bool:
        return uses_litellm_compatible_schema(
            provider="litellm",
            model_id=self.config.get("model_id", ""),
            api_base=self._litellm_api_base,
        )

    def _request_tool_parameters(self, tool_spec: ToolSpec) -> dict[str, Any]:
        parameters = copy.deepcopy(tool_spec["inputSchema"]["json"])
        if not self._uses_provider_tool_schema_hints:
            return parameters
        tool_name = str(tool_spec.get("name") or "")
        if tool_name == "secure_cli":
            return self._secure_cli_parameters(parameters)
        if tool_name in _RUNTIME_SETTINGS_SCHEMA_HINT_TOOL_NAMES:
            return self._runtime_settings_parameters(tool_name, parameters)
        return parameters

    @staticmethod
    def _runtime_settings_parameters(
        tool_name: str,
        parameters: dict[str, Any],
    ) -> dict[str, Any]:
        properties = parameters.get("properties")
        if not isinstance(properties, dict):
            return parameters

        if tool_name == "plan_runtime_setting_change":
            properties["value"] = {
                "type": "string",
                "description": _RUNTIME_SETTING_VALUE_DESCRIPTION,
            }
            return parameters

        changes_schema = properties.get("changes")
        definitions = parameters.get("$defs")
        if not isinstance(changes_schema, dict) or not isinstance(definitions, dict):
            return parameters
        items_schema = changes_schema.get("items")
        if not isinstance(items_schema, dict):
            return parameters
        reference = str(items_schema.get("$ref") or "")
        definition_name = reference.rsplit("/", 1)[-1] if reference.startswith("#/$defs/") else ""
        change_schema = definitions.get(definition_name)
        if not isinstance(change_schema, dict):
            return parameters

        provider_change_schema = copy.deepcopy(change_schema)
        provider_change_schema.pop("title", None)
        change_properties = provider_change_schema.get("properties")
        if not isinstance(change_properties, dict):
            return parameters
        path_schema = change_properties.get("path")
        if isinstance(path_schema, dict):
            path_schema.pop("title", None)
        change_properties["value"] = {
            "type": "string",
            "description": _RUNTIME_SETTING_VALUE_DESCRIPTION,
        }
        changes_schema["items"] = provider_change_schema
        parameters.pop("$defs", None)
        return parameters

    @staticmethod
    def _secure_cli_parameters(parameters: dict[str, Any]) -> dict[str, Any]:
        properties = parameters.setdefault("properties", {})
        if not isinstance(properties, dict):
            return parameters
        provider_schema = properties.get("provider")
        if isinstance(provider_schema, dict):
            provider_schema["description"] = (
                "Required secure broker provider. Common values include credential_catalog for saved credential "
                "metadata and OpenShell account use, secure_requests for brokered HTTP, and google for Google "
                "Workspace actions."
            )
        action_schema = properties.get("action")
        if isinstance(action_schema, dict):
            action_schema["description"] = (
                "Required provider action. Credential examples: credential.get_metadata and "
                "credential.bind_for_openshell. HTTP examples: request, get, post. "
                "Google examples: gmail_search, gmail_read, gmail_send, and gog.exec."
            )
        params_schema = properties.get("params")
        if not isinstance(params_schema, dict):
            return parameters
        params_schema["type"] = "object"
        params_schema["additionalProperties"] = True
        params_schema["description"] = SECURE_CLI_COMPAT_PARAMS_DESCRIPTION
        params_properties = params_schema.setdefault("properties", {})
        if not isinstance(params_properties, dict):
            params_properties = {}
            params_schema["properties"] = params_properties
        params_properties.setdefault(
            "query",
            {
                "type": "string",
                "description": (
                    "For google.gmail_search, calendar_list, or drive_list: search/filter query. "
                    "For Gmail inbox checks use a Gmail query such as in:inbox newer_than:1d."
                ),
            },
        )
        params_properties.setdefault(
            "limit",
            {
                "type": "integer",
                "description": "For Google list/search actions: maximum result count.",
            },
        )
        params_properties.setdefault(
            "id",
            {
                "type": "string",
                "description": "For google.gmail_read or export/read actions: message, thread, file, document, or sheet id.",
            },
        )
        params_properties.setdefault(
            "argv",
            {
                "type": "array",
                "items": {"type": "string"},
                "description": (
                    "For google.gog.exec only: arguments after the gog executable, as a JSON array/list. "
                    "Do not include the gog executable and do not pass a shell string. Put options after "
                    "the full command; local files must be in this task's folders."
                ),
            },
        )
        params_properties.setdefault(
            "json",
            {
                "type": "boolean",
                "description": "For google.gog.exec only: set false when argv requests plain decoded output.",
            },
        )
        params_properties.setdefault(
            "to",
            {
                "type": "string",
                "description": "For google.gmail_send: recipient email address.",
            },
        )
        params_properties.setdefault(
            "subject",
            {
                "type": "string",
                "description": "For google.gmail_send: email subject.",
            },
        )
        params_properties.setdefault(
            "body",
            {
                "type": "string",
                "description": "For google.gmail_send: full email body.",
            },
        )
        params_properties.setdefault(
            "calendar_id",
            {
                "type": "string",
                "description": "For google.calendar_list or calendar_create: calendar id, usually primary.",
            },
        )
        params_properties.setdefault(
            "from",
            {
                "type": "string",
                "description": "For google.calendar_create: event start datetime.",
            },
        )
        params_properties.setdefault(
            "format",
            {
                "type": "string",
                "description": "For google.gmail_read or export actions: requested output format.",
            },
        )
        params_properties.setdefault(
            "path",
            {
                "type": "string",
                "description": "For Google Drive/Docs/Sheets actions: output path, folder path, parent alias, or legacy sheet range alias.",
            },
        )
        params_properties.setdefault(
            "range",
            {
                "type": "string",
                "description": "For google.sheets_read: spreadsheet range such as Sheet1!A1:D20.",
            },
        )
        params_properties.setdefault(
            "credential_key",
            {
                "type": "string",
                "description": (
                    "Stable catalog credential key for credential_catalog actions such as credential.get_metadata, "
                    "for example porkbun_api. Put it inside params."
                ),
            },
        )
        params_properties.setdefault(
            "tool_id",
            {
                "type": "string",
                "description": (
                    "For credential.bind_for_openshell: the web API tool whose linked account code in the "
                    "OpenShell sandbox should use, for example http:github."
                ),
            },
        )
        params_properties.setdefault(
            "provider_type",
            {
                "type": "string",
                "description": "For credential.bind_for_openshell: OpenShell provider type; omit to use generic.",
            },
        )
        params_properties.setdefault(
            "url",
            {
                "type": "string",
                "description": "Absolute http(s) URL for secure_requests actions.",
            },
        )
        params_properties.setdefault(
            "method",
            {
                "type": "string",
                "description": "HTTP method for secure_requests.request; omit for get/post/etc. actions.",
            },
        )
        params_properties.setdefault(
            "headers",
            {
                "type": "object",
                "additionalProperties": {"type": "string"},
                "description": (
                    "HTTP headers for secure_requests. Leave out credentials: the broker adds the headers of "
                    "the account linked to the web API tool."
                ),
            },
        )
        params_properties.setdefault(
            "json_body",
            {
                "type": "object",
                "additionalProperties": True,
                "description": (
                    "JSON request body for secure_requests, without credentials. Use this instead of json when "
                    "the google.gog.exec json boolean is also advertised."
                ),
            },
        )
        params_properties.setdefault(
            "timeout",
            {
                "type": "integer",
                "description": "Optional secure_requests timeout in seconds.",
            },
        )
        return parameters

    def _should_replay_reasoning_for_message(self, contents: list[ContentBlock]) -> bool:
        if self._reasoning_replay_mode != "deepseek_tool_calls":
            return False
        return any("toolUse" in content for content in contents)

    @staticmethod
    def _tool_use_ids(contents: list[ContentBlock]) -> set[str]:
        tool_use_ids: set[str] = set()
        for content in contents:
            tool_use = content.get("toolUse")
            if isinstance(tool_use, dict) and tool_use.get("toolUseId"):
                tool_use_ids.add(str(tool_use["toolUseId"]))
        return tool_use_ids

    @staticmethod
    def _tool_result_id(content: ContentBlock) -> str | None:
        tool_result = content.get("toolResult")
        if isinstance(tool_result, dict) and tool_result.get("toolUseId"):
            return str(tool_result["toolUseId"])
        return None

    def _messages_after_reasoning_pruning(self, messages: Messages) -> Messages:
        retention = self.reasoning_trace_retention_messages
        if retention is None or self._reasoning_replay_mode != "deepseek_tool_calls":
            return messages

        last_index = len(messages) - 1
        pruned_message_indexes: set[int] = set()
        pruned_tool_use_ids: set[str] = set()
        for index, message in enumerate(messages):
            contents = list(cast(list[ContentBlock], message["content"]))
            if not self._should_replay_reasoning_for_message(contents):
                continue
            if not any("reasoningContent" in content for content in contents):
                continue
            if last_index - index <= retention:
                continue
            tool_use_ids = self._tool_use_ids(contents)
            if tool_use_ids:
                pruned_message_indexes.add(index)
                pruned_tool_use_ids.update(tool_use_ids)

        if not pruned_message_indexes:
            return messages

        pruned_messages: list[dict[str, Any]] = []
        for index, message in enumerate(messages):
            if index in pruned_message_indexes:
                continue

            contents = list(cast(list[ContentBlock], message["content"]))
            if pruned_tool_use_ids and any("toolResult" in content for content in contents):
                retained_contents = [
                    content
                    for content in contents
                    if (tool_result_id := self._tool_result_id(content)) is None
                    or tool_result_id not in pruned_tool_use_ids
                ]
                if not retained_contents:
                    continue
                if len(retained_contents) != len(contents):
                    message = {**message, "content": retained_contents}

            pruned_messages.append(cast(dict[str, Any], message))

        logger.debug(
            "Pruned %s stale DeepSeek reasoning/tool-call exchange message(s) for LiteLLM model %s.",
            len(pruned_message_indexes),
            self.config.get("model_id"),
        )
        return cast(Messages, pruned_messages)

    @staticmethod
    def _reasoning_text(content: ContentBlock) -> str | None:
        reasoning = content.get("reasoningContent")
        if not reasoning:
            return None
        reasoning_text = reasoning.get("reasoningText")
        if isinstance(reasoning_text, dict):
            text = reasoning_text.get("text")
            return str(text) if text is not None else None
        return None

    @classmethod
    def _assistant_text_content(cls, contents: list[ContentBlock]) -> str:
        parts: list[str] = []
        for content in contents:
            if "text" in content:
                parts.append(str(content["text"]))
            elif "citationsContent" in content:
                citations_content = content.get("citationsContent") or {}
                for item in citations_content.get("content", []) or []:
                    if "text" in item:
                        parts.append(str(item["text"]))
        return "\n".join(part for part in parts if part)

    def _format_regular_messages_for_instance(self, messages: Messages) -> list[dict[str, Any]]:
        formatted_messages: list[dict[str, Any]] = []

        for message in messages:
            contents = list(cast(list[ContentBlock], message["content"]))
            replay_reasoning = self._should_replay_reasoning_for_message(contents)

            filtered_contents: list[ContentBlock] = []
            reasoning_parts: list[str] = []
            dropped_reasoning = False
            for content in contents:
                if "reasoningContent" in content:
                    if replay_reasoning:
                        reasoning_text = self._reasoning_text(content)
                        if reasoning_text:
                            reasoning_parts.append(reasoning_text)
                        else:
                            logger.warning(
                                "Dropping non-text reasoningContent for LiteLLM model %s because it cannot be "
                                "serialized as DeepSeek reasoning_content.",
                                self.config.get("model_id"),
                            )
                    else:
                        dropped_reasoning = True
                    continue
                if any(block_type in content for block_type in ["toolResult", "toolUse"]):
                    continue
                if _has_location_source(content):
                    logger.warning(
                        "Location sources are not supported by LiteLLM/OpenAI-compatible models; "
                        "skipping content block"
                    )
                    continue
                filtered_contents.append(content)

            if dropped_reasoning:
                logger.debug(
                    "Dropping reasoningContent for LiteLLM model %s because no reasoning replay compatibility rule matched.",
                    self.config.get("model_id"),
                )

            formatted_contents = [self.format_request_message_content(content) for content in filtered_contents]
            formatted_tool_calls = [
                self.format_request_message_tool_call(content["toolUse"])
                for content in contents
                if "toolUse" in content
            ]
            formatted_tool_messages = [
                self.format_request_tool_message(content["toolResult"])
                for content in contents
                if "toolResult" in content
            ]

            formatted_message: dict[str, Any] = {"role": message["role"]}
            if formatted_tool_calls:
                # DeepSeek thinking-mode tool turns require assistant content to
                # be present alongside reasoning_content and tool_calls.
                formatted_message["content"] = self._assistant_text_content(contents)
            elif formatted_contents:
                formatted_message["content"] = formatted_contents
            if formatted_tool_calls:
                formatted_message["tool_calls"] = formatted_tool_calls
            if replay_reasoning and reasoning_parts:
                formatted_message["reasoning_content"] = "".join(reasoning_parts)
            formatted_messages.append(formatted_message)

            user_messages_with_images: list[dict[str, Any]] = []
            for tool_msg in formatted_tool_messages:
                tool_msg_clean, user_msg_with_images = self._split_tool_message_images(tool_msg)
                formatted_messages.append(tool_msg_clean)
                if user_msg_with_images:
                    user_messages_with_images.append(user_msg_with_images)
            formatted_messages.extend(user_messages_with_images)

        return formatted_messages

    def _format_request_messages_for_instance(
        self,
        messages: Messages,
        system_prompt: str | None = None,
        *,
        system_prompt_content: list[SystemContentBlock] | None = None,
    ) -> list[dict[str, Any]]:
        messages = self._messages_after_reasoning_pruning(messages)
        formatted_messages = self._format_system_messages(
            system_prompt,
            system_prompt_content=system_prompt_content,
        )
        formatted_messages.extend(self._format_regular_messages_for_instance(messages))
        return [message for message in formatted_messages if "content" in message or "tool_calls" in message]

    def format_request(
        self,
        messages: Messages,
        tool_specs: list[ToolSpec] | None = None,
        system_prompt: str | None = None,
        tool_choice: ToolChoice | None = None,
        *,
        system_prompt_content: list[SystemContentBlock] | None = None,
        **kwargs: Any,
    ) -> dict[str, Any]:
        """Format a LiteLLM request while preserving required reasoning fields."""

        return {
            "messages": self._format_request_messages_for_instance(
                messages,
                system_prompt,
                system_prompt_content=system_prompt_content,
            ),
            "model": self.config["model_id"],
            "stream": True,
            "stream_options": {"include_usage": True},
            "tools": [
                {
                    "type": "function",
                    "function": {
                        "name": tool_spec["name"],
                        "description": tool_spec["description"],
                        "parameters": self._request_tool_parameters(tool_spec),
                    },
                }
                for tool_spec in tool_specs or []
            ],
            **self._format_request_tool_choice(tool_choice),
            # The factory stores None when a model has no parameters (no timeout, temperature or limit).
            **cast(dict[str, Any], self.config.get("params") or {}),
        }

    async def _process_choice_content(
        self,
        choice: Any,
        data_type: Any = None,
        tool_calls: dict[int, list[Any]] | None = None,
        is_streaming: bool = True,
    ):
        """Compatibility shim for older tests and SDK versions.

        Strands SDK versions before the current streaming formatter exposed
        ``_process_choice_content`` on OpenAI-compatible models. The newer SDK
        routes through ``format_chunk`` instead. This narrow shim keeps the
        repo-owned reasoning adapter behavior testable without depending on the
        removed private method.
        """

        async for updated_data_type, event in super()._process_choice_content(
            choice,
            data_type,
            tool_calls if tool_calls is not None else {},
            is_streaming,
        ):
            yield updated_data_type, event
