"""Strands-native runtime guardrails for tool-loop behavior."""

from __future__ import annotations

import json
import logging
from typing import Any, Dict, Optional, get_args

from strands.hooks import AfterModelCallEvent, AfterToolCallEvent, HookRegistry

from custom_tools.runtime_context import caller_invocation_state
from custom_agents.structured_llm_messages import (
    CompatibleTurnActionType,
    CompatibleWorkerTurnDecision,
    ModelWorkerTurnDecision,
)

logger = logging.getLogger(__name__)

MODEL_TURN_DECISION_TOOL_NAMES = {
    "CompatibleModelAgentTurnDecision",
    "FireworksModelAgentTurnDecision",
    "ModelAgentTurnDecision",
    # Specialists' turn answers (Strands names the tool after the model class).
    CompatibleWorkerTurnDecision.__name__,
    ModelWorkerTurnDecision.__name__,
}

STRUCTURED_OUTPUT_TOOL_NAMES = {
    *MODEL_TURN_DECISION_TOOL_NAMES,
    "StructuredAgentTurnDecision",
    "StructuredLLMResponse",
}

MODEL_ACTION_PAYLOAD_FIELD_BY_TYPE = {
    "delegate_task": "task",
    "task_response": "task",
    "task_query": "task_query",
    "schedule_message": "scheduled_message",
    "secure_credential_collection_request": "secure_credential_collection_request",
    "signup_automation_request": "signup_automation_request",
}

LEGACY_PAYLOAD_FIELD_BY_MESSAGE_TYPE = {
    "AgentStatus": "agent_status",
    "ControlInstruction": "control_instruction",
    "NoOpMessage": "no_op_message",
    "ScheduledMessage": "scheduled_message",
    "SecureCredentialCollectionRequest": "secure_credential_collection_request",
    "SecureCredentialCollectionResult": "secure_credential_collection_result",
    "SignupAutomationRequest": "signup_automation_request",
    "SignupAutomationResult": "signup_automation_result",
    "SystemStatusMessage": "system_status_message",
    "Task": "task",
    "TaskQuery": "task_query",
}

RUNTIME_OWNED_ID_FIELDS = {
    "execution_history",
    "message_class",
    "message_id",
    "outbox_action_id",
    "parent_task_id",
    "routing_path",
    "task_id",
    "task_query_id",
    "task_state",
    "to_routing_path",
}


def _compact_debug_preview(value: Any, *, max_chars: int = 1200) -> str:
    try:
        text = json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)
    except (TypeError, ValueError):
        text = repr(value)
    if len(text) <= max_chars:
        return text
    return text[:max_chars] + f"...<truncated {len(text) - max_chars} chars>"


def _budget_pressure_level(ratio: float) -> Optional[int]:
    if ratio >= 0.95:
        return 95
    if ratio >= 0.90:
        return 90
    if ratio >= 0.75:
        return 75
    return None


def _budget_pressure_label(level: int) -> str:
    if level >= 95:
        return "critical"
    if level >= 90:
        return "nearly exhausted"
    return "elevated"


def _assistant_message_metrics(message: Any) -> Dict[str, int]:
    metrics = {
        "text_chars": 0,
        "reasoning_chars": 0,
        "tool_use_count": 0,
    }
    if not isinstance(message, dict):
        return metrics
    content = message.get("content")
    if not isinstance(content, list):
        return metrics
    for block in content:
        if not isinstance(block, dict):
            continue
        text = block.get("text")
        if text:
            metrics["text_chars"] += len(str(text))
        if "toolUse" in block:
            metrics["tool_use_count"] += 1
        reasoning = block.get("reasoningContent")
        if not isinstance(reasoning, dict):
            continue
        reasoning_text = reasoning.get("reasoningText")
        if isinstance(reasoning_text, dict):
            reasoning_text = reasoning_text.get("text")
        if reasoning_text is None:
            reasoning_text = reasoning.get("text")
        if reasoning_text:
            metrics["reasoning_chars"] += len(str(reasoning_text))
    return metrics


def _append_text_to_tool_result(result: Any, text: str) -> Any:
    if not isinstance(result, dict):
        return result
    content = result.get("content")
    if not isinstance(content, list):
        content = []
    else:
        content = list(content)
    content.append({"text": text})
    updated = dict(result)
    updated["content"] = content
    return updated


# Actions of the structured turn answer. Some models call one as if it were a
# tool; the call finds no tool, and the hint below says where it belongs.
TURN_ACTION_TYPES = frozenset(get_args(CompatibleTurnActionType))


def _action_called_as_tool_hint(action_type: str) -> str:
    return (
        f"{action_type} is not a tool. It is an action_type of your turn answer: finish this turn by "
        f"returning your turn decision with an action whose action_type is \"{action_type}\" "
        "and its payload (for delegate_task, a task with problem_statement plus the target agent type). "
        "Do not call it again as a tool."
    )


def _model_turn_decision_repair_hint(tool_name: str, input_diagnostics: Dict[str, Any]) -> Optional[str]:
    if tool_name not in MODEL_TURN_DECISION_TOOL_NAMES:
        return None
    if input_diagnostics.get("input_type") != "dict":
        return None

    top_level_keys = input_diagnostics.get("top_level_keys")
    if not isinstance(top_level_keys, list):
        top_level_keys = []
    action_count = input_diagnostics.get("action_count")
    actions_missing = "actions" not in top_level_keys
    actions_empty = action_count == 0
    actions_not_a_list = "actions" in top_level_keys and action_count is None
    if not (actions_missing or actions_empty or actions_not_a_list):
        return None

    warning = {
        "status": "warning",
        "warning_stage": "structured_output_repair_hint",
        "message": (
            f"Your {tool_name} tool input did not include the required non-empty actions list. "
            f"Call {tool_name} again with at least one ordered action. For a normal user-visible "
            "completion, set actions[0].action_type to task_response and put the answer in "
            "actions[0].task.answer. If you wrote, updated, or saved a file for the user, still finish "
            "with a task_response that summarizes the path and result."
        ),
        "mitigation_hints": [
            "populate actions with one or more ordered runtime actions",
            "for ordinary replies use action_type=task_response",
            "put user-visible prose in actions[0].task.answer",
            "do not send top-level prose or handwritten JSON",
        ],
    }
    return json.dumps(warning, sort_keys=True)


class RuntimeGuardrailsPlugin:
    """Bundle model-visible tool-loop guardrails as a Strands plugin."""

    name = "runtime_guardrails"

    _STRUCTURED_COUNT_KEY = "structured_output_failure_count"
    _STRUCTURED_FALLBACK_KEY = "structured_output_fallback"
    _STRUCTURED_LAST_INPUT_KEY = "structured_output_last_input"
    _STRUCTURED_FORCE_COUNT_KEY = "structured_output_forced_retry_count"
    _STRUCTURED_FORCE_LAST_KEY = "structured_output_forced_retry_last"
    _TURN_DECISION_CONTEXT_KEY = "turn_decision_structured_output_context"

    _TOOL_CALL_COUNT_KEY = "tool_call_budget_count"
    _TOOL_CALL_WARNING_LEVEL_KEY = "tool_call_budget_warning_level"
    _TOOL_CALL_KEYS_KEY = "tool_call_budget_tool_keys"

    def __init__(
        self,
        *,
        structured_output_failure_threshold: Optional[int] = None,
        max_tool_calls: Optional[int] = None,
        logger_: Optional[logging.Logger] = None,
    ) -> None:
        self.structured_output_failure_threshold = structured_output_failure_threshold
        self.max_tool_calls = max_tool_calls
        self.logger = logger_ or logger

    def register_hooks(self, registry: HookRegistry, **_: Any) -> None:
        registry.add_callback(AfterModelCallEvent, self.after_model_call)
        registry.add_callback(AfterToolCallEvent, self.after_tool_call)

    def after_model_call(self, event: AfterModelCallEvent) -> None:
        stop_response = event.stop_response
        if stop_response is None or stop_response.stop_reason != "end_turn":
            return
        turn_context = self._turn_decision_context(event.invocation_state)
        if turn_context is None:
            return

        request_state = event.invocation_state.get("request_state")
        if not isinstance(request_state, dict):
            request_state = {}
            event.invocation_state["request_state"] = request_state
        forced_retry_exhausted = bool(request_state.get(self._STRUCTURED_FORCE_COUNT_KEY))
        forced_retry_count = 1
        request_state[self._STRUCTURED_FORCE_COUNT_KEY] = forced_retry_count
        message_metrics = _assistant_message_metrics(stop_response.message)
        details = {
            "forced_retry_count": forced_retry_count,
            "forced_retry_exhausted": forced_retry_exhausted,
            "stop_reason": stop_response.stop_reason,
            "expected_tool_name": turn_context.get("expected_tool_name"),
            **message_metrics,
        }
        request_state[self._STRUCTURED_FORCE_LAST_KEY] = details
        event_name = (
            "structured_output_forced_retry_exhausted"
            if forced_retry_exhausted
            else "structured_output_forced_retry"
        )
        self.logger.warning(
            "[%s] event=%s task_id=%s correlation_id=%s conversation_id=%s "
            "named_agent_type=%s provider=%s model_id=%s expected_tool_name=%s "
            "forced_retry_count=%s forced_retry_exhausted=%s assistant_text_chars=%s "
            "assistant_reasoning_chars=%s "
            "assistant_tool_use_count=%s",
            turn_context.get("agent_id") or "unknown",
            event_name,
            turn_context.get("task_id") or "unknown",
            turn_context.get("correlation_id") or "unknown",
            turn_context.get("conversation_id") or "unknown",
            turn_context.get("named_agent_type") or "unknown",
            turn_context.get("provider") or "unknown",
            turn_context.get("model_id") or "unknown",
            turn_context.get("expected_tool_name") or "unknown",
            forced_retry_count,
            forced_retry_exhausted,
            message_metrics["text_chars"],
            message_metrics["reasoning_chars"],
            message_metrics["tool_use_count"],
        )

    def after_tool_call(self, event: AfterToolCallEvent) -> None:
        if self._is_structured_output_tool(event):
            self._handle_structured_output_result(event)
            return
        self._explain_action_called_as_tool(event)
        self._handle_tool_call_budget(event)

    @staticmethod
    def _explain_action_called_as_tool(event: AfterToolCallEvent) -> None:
        name = str(event.tool_use.get("name") or "")
        if event.selected_tool is not None or name not in TURN_ACTION_TYPES:
            return
        logger.info("The model called a turn action as a tool. event=turn_action_called_as_tool action_type=%s", name)
        event.result = _append_text_to_tool_result(event.result, _action_called_as_tool_hint(name))

    @classmethod
    def _turn_decision_context(cls, invocation_state: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        context = caller_invocation_state(invocation_state).get(cls._TURN_DECISION_CONTEXT_KEY)
        return context if isinstance(context, dict) else None

    def _handle_structured_output_result(self, event: AfterToolCallEvent) -> None:
        request_state = event.invocation_state.setdefault("request_state", {})
        input_diagnostics = self._input_diagnostics(event)
        tool_name = self._tool_name(event)
        request_state[self._STRUCTURED_LAST_INPUT_KEY] = {
            "tool_name": self._tool_name(event),
            "tool_use_id": event.tool_use.get("toolUseId"),
            "input_diagnostics": input_diagnostics,
            "input_preview": _compact_debug_preview(event.tool_use.get("input"), max_chars=2000),
        }
        result = event.result if isinstance(event.result, dict) else {}
        status = result.get("status")
        if status != "error":
            if self._resolved_threshold() is not None:
                request_state[self._STRUCTURED_COUNT_KEY] = 0
            return

        repair_hint = _model_turn_decision_repair_hint(tool_name, input_diagnostics)
        if repair_hint:
            event.result = _append_text_to_tool_result(event.result, repair_hint)

        threshold = self._resolved_threshold()
        if threshold is None:
            return

        observed_failures = int(request_state.get(self._STRUCTURED_COUNT_KEY) or 0) + 1
        request_state[self._STRUCTURED_COUNT_KEY] = observed_failures
        error_preview = _compact_debug_preview(result.get("content"), max_chars=1200)
        input_preview = _compact_debug_preview(event.tool_use.get("input"), max_chars=1600)
        self.logger.warning(
            "%s structured-output validation failure %s/%s: toolUseId=%s error=%s input_diagnostics=%s",
            tool_name,
            observed_failures,
            threshold,
            event.tool_use.get("toolUseId"),
            error_preview,
            _compact_debug_preview(input_diagnostics, max_chars=1600),
        )
        if observed_failures >= threshold:
            request_state["stop_event_loop"] = True
            request_state[self._STRUCTURED_FALLBACK_KEY] = {
                "reason": "structured_output_validation_failures",
                "threshold": threshold,
                "observed_failures": observed_failures,
                "tool_name": tool_name,
                "last_error_preview": error_preview,
                "last_input_diagnostics": input_diagnostics,
                "last_input_preview": input_preview,
            }

    def _handle_tool_call_budget(self, event: AfterToolCallEvent) -> None:
        max_tool_calls = self._resolved_limit(self.max_tool_calls)
        if max_tool_calls is None:
            return
        request_state = event.invocation_state.setdefault("request_state", {})
        tool_key = self._tool_key(event)
        seen_keys = request_state.setdefault(self._TOOL_CALL_KEYS_KEY, [])
        if tool_key in seen_keys:
            return
        seen_keys.append(tool_key)
        observed = int(request_state.get(self._TOOL_CALL_COUNT_KEY) or 0) + 1
        request_state[self._TOOL_CALL_COUNT_KEY] = observed
        ratio = observed / max_tool_calls
        level = _budget_pressure_level(ratio)
        emitted_level = int(request_state.get(self._TOOL_CALL_WARNING_LEVEL_KEY) or 0)
        if level is None or level <= emitted_level:
            return
        request_state[self._TOOL_CALL_WARNING_LEVEL_KEY] = level
        percent = round(ratio * 100)
        warning = {
            "status": "warning",
            "warning_stage": "tool_call_budget_pressure",
            "warning_level": level,
            "message": (
                f"Tool call budget pressure is {_budget_pressure_label(level)}. "
                f"{percent}% ({observed} / {max_tool_calls} (max) tool calls consumed); "
                f"crossed the {level}% warning threshold. If budget warnings begin, summarize current findings instead of trying alternate retrieval paths."
            ),
            "observed_tool_calls": observed,
            "max_tool_calls": max_tool_calls,
            "mitigation_hints": [
                "summarize current findings instead of trying alternate retrieval paths",
                "avoid new broad searches or fetches",
                "return a terminal Task response when you have enough evidence",
            ],
        }
        self.logger.warning(
            "Tool call budget pressure warning: toolUseId=%s observed=%s max=%s level=%s",
            event.tool_use.get("toolUseId"),
            observed,
            max_tool_calls,
            level,
        )
        event.result = _append_text_to_tool_result(event.result, json.dumps(warning, sort_keys=True))

    def _resolved_threshold(self) -> Optional[int]:
        if self.structured_output_failure_threshold is None:
            return None
        try:
            threshold = int(self.structured_output_failure_threshold)
        except (TypeError, ValueError):
            return None
        return threshold if threshold > 0 else None

    @staticmethod
    def _resolved_limit(raw_value: Optional[int]) -> Optional[int]:
        if raw_value is None:
            return None
        try:
            value = int(raw_value)
        except (TypeError, ValueError):
            return None
        return value if value > 0 else None

    @staticmethod
    def _is_structured_output_tool(event: AfterToolCallEvent) -> bool:
        selected_tool = event.selected_tool
        if getattr(selected_tool, "tool_type", None) == "structured_output":
            return True
        if getattr(selected_tool, "tool_name", None) in STRUCTURED_OUTPUT_TOOL_NAMES:
            return True
        return event.tool_use.get("name") in STRUCTURED_OUTPUT_TOOL_NAMES

    @staticmethod
    def _tool_name(event: AfterToolCallEvent) -> str:
        return str(
            event.tool_use.get("name")
            or getattr(event.selected_tool, "tool_name", None)
            or "structured_output"
        )

    @staticmethod
    def _tool_key(event: AfterToolCallEvent) -> str:
        tool_use_id = event.tool_use.get("toolUseId")
        if tool_use_id:
            return f"id:{tool_use_id}"
        return f"name:{event.tool_use.get('name') or 'unknown'}"

    @staticmethod
    def _input_diagnostics(event: AfterToolCallEvent) -> Dict[str, Any]:
        tool_input = event.tool_use.get("input")
        diagnostics: Dict[str, Any] = {
            "input_type": type(tool_input).__name__,
            "selected_message_type": None,
            "expected_payload_field": None,
            "has_expected_payload_field": None,
            "populated_payload_fields": [],
            "top_level_keys": None,
            "top_level_routing": {},
            "nested_task_keys": None,
            "nested_task_routing": {},
            "nested_task_potentially_ignored_fields": [],
            "likely_misnested_task_fields": [],
            "action_count": None,
            "action_types": [],
            "missing_action_payloads": [],
            "action_payload_keys": [],
            "task_payload_keys": [],
            "runtime_owned_id_fields": [],
        }
        if not isinstance(tool_input, dict):
            diagnostics["input_preview"] = _compact_debug_preview(tool_input, max_chars=600)
            return diagnostics

        actions = tool_input.get("actions")
        selected_message_type = tool_input.get("selected_message_type")
        if isinstance(actions, list):
            RuntimeGuardrailsPlugin._add_turn_decision_diagnostics(diagnostics, actions)
            expected_payload_field = "actions"
            payload_fields = {"actions"}
        else:
            expected_payload_field = LEGACY_PAYLOAD_FIELD_BY_MESSAGE_TYPE.get(str(selected_message_type))
            payload_fields = set(LEGACY_PAYLOAD_FIELD_BY_MESSAGE_TYPE.values())
        top_level_keys = sorted(str(key) for key in tool_input.keys())

        diagnostics["selected_message_type"] = selected_message_type
        diagnostics["expected_payload_field"] = expected_payload_field
        diagnostics["top_level_keys"] = top_level_keys
        diagnostics["runtime_owned_id_fields"].extend(
            sorted(str(key) for key in RUNTIME_OWNED_ID_FIELDS.intersection(tool_input.keys()))
        )
        diagnostics["top_level_routing"] = {
            "target_named_agent_type": tool_input.get("target_named_agent_type"),
            "target_agent_name": tool_input.get("target_agent_name"),
            "is_response_requested": tool_input.get("is_response_requested"),
            "msg_meta.to_named_agent_type": (
                tool_input.get("msg_meta", {}).get("to_named_agent_type")
                if isinstance(tool_input.get("msg_meta"), dict)
                else None
            ),
            "msg_meta.to_agent_name": (
                tool_input.get("msg_meta", {}).get("to_agent_name")
                if isinstance(tool_input.get("msg_meta"), dict)
                else None
            ),
        }
        diagnostics["populated_payload_fields"] = sorted(
            field_name
            for field_name in payload_fields
            if tool_input.get(field_name) is not None
        )
        if expected_payload_field:
            diagnostics["has_expected_payload_field"] = tool_input.get(expected_payload_field) is not None
        if selected_message_type == "Task" and tool_input.get("task") is None:
            task_field_names = {
                "answer",
                "errors",
                "parent_task_id",
                "problem_statement",
                "task_id",
                "task_plan",
                "task_type",
                "thoughts_info",
            }
            diagnostics["likely_misnested_task_fields"] = sorted(task_field_names.intersection(tool_input.keys()))
        task_payload = tool_input.get("task")
        if isinstance(task_payload, dict):
            diagnostics["nested_task_keys"] = sorted(str(key) for key in task_payload.keys())
            diagnostics["task_payload_keys"].append(
                {"path": "task", "keys": diagnostics["nested_task_keys"]}
            )
            diagnostics["runtime_owned_id_fields"].extend(
                f"task.{key}"
                for key in sorted(str(key) for key in RUNTIME_OWNED_ID_FIELDS.intersection(task_payload.keys()))
            )
            diagnostics["nested_task_routing"] = {
                "target_named_agent_type": task_payload.get("target_named_agent_type"),
                "target_agent_name": task_payload.get("target_agent_name"),
                "to_named_agent_type": task_payload.get("to_named_agent_type"),
                "to_agent_name": task_payload.get("to_agent_name"),
                "is_response_requested": task_payload.get("is_response_requested"),
                "msg_meta.to_named_agent_type": (
                    task_payload.get("msg_meta", {}).get("to_named_agent_type")
                    if isinstance(task_payload.get("msg_meta"), dict)
                    else None
                ),
                "msg_meta.to_agent_name": (
                    task_payload.get("msg_meta", {}).get("to_agent_name")
                    if isinstance(task_payload.get("msg_meta"), dict)
                    else None
                ),
            }
            routing_related_fields = {
                "target_named_agent_type",
                "target_agent_name",
                "to_named_agent_type",
                "to_agent_name",
                "is_response_requested",
                "requester_type",
                "msg_meta",
            }
            diagnostics["nested_task_potentially_ignored_fields"] = sorted(
                routing_related_fields.intersection(task_payload.keys())
            )
        diagnostics["runtime_owned_id_fields"] = sorted(set(diagnostics["runtime_owned_id_fields"]))
        return diagnostics

    @staticmethod
    def _add_turn_decision_diagnostics(diagnostics: Dict[str, Any], actions: list[Any]) -> None:
        diagnostics["action_count"] = len(actions)
        for index, action in enumerate(actions):
            if not isinstance(action, dict):
                diagnostics["missing_action_payloads"].append(
                    {"index": index, "reason": f"action is {type(action).__name__}"}
                )
                continue

            action_type = action.get("action_type")
            diagnostics["action_types"].append(str(action_type or "missing"))
            expected_payload_field = MODEL_ACTION_PAYLOAD_FIELD_BY_TYPE.get(str(action_type))
            if expected_payload_field and action.get(expected_payload_field) is None:
                diagnostics["missing_action_payloads"].append(
                    {
                        "index": index,
                        "action_type": action_type,
                        "expected_payload_field": expected_payload_field,
                    }
                )

            action_runtime_fields = sorted(str(key) for key in RUNTIME_OWNED_ID_FIELDS.intersection(action.keys()))
            diagnostics["runtime_owned_id_fields"].extend(
                f"actions[{index}].{field_name}" for field_name in action_runtime_fields
            )

            payload = action.get(expected_payload_field) if expected_payload_field else None
            if isinstance(payload, dict):
                payload_keys = sorted(str(key) for key in payload.keys())
                diagnostics["action_payload_keys"].append(
                    {
                        "index": index,
                        "action_type": action_type,
                        "payload_field": expected_payload_field,
                        "keys": payload_keys,
                    }
                )
                payload_runtime_fields = sorted(
                    str(key) for key in RUNTIME_OWNED_ID_FIELDS.intersection(payload.keys())
                )
                diagnostics["runtime_owned_id_fields"].extend(
                    f"actions[{index}].{expected_payload_field}.{field_name}"
                    for field_name in payload_runtime_fields
                )
                if expected_payload_field == "task":
                    diagnostics["task_payload_keys"].append(
                        {"path": f"actions[{index}].task", "keys": payload_keys}
                    )
