"""One-step ``call_tool(name, inputs)`` rewriting and unwrapping for the tool index.

Why: model APIs only accept calls to tools present in the model's tool list.
With ``agent.tool_presentation`` set to ``index`` or ``index_and_scripts``,
most held tools wait in a one-line prompt index rather than carrying full
definitions on every request. ``call_tool`` is one always-shown tool in those
modes: ``CallToolHook`` rewrites ``call_tool(name=F, inputs=...)`` into a
call to ``F`` (preserving ``toolUseId``, rewriting ``event.tool_use`` to a
fresh dict, and setting ``event.selected_tool``) before any other
``BeforeToolCallEvent`` hook runs.

Because ``CallToolHook`` runs first (``CALL_TOOL_HOOK_ORDER = -200.0``, before
Strands' ``HookOrder.SDK_FIRST`` at ``-100``):

- ``OnRequestToolHook`` sees ``selected_tool`` already set for offered catalog
  functions, so it does not load the function's full group into later model
  calls, and handles unknown typos with closest real names;
- ``RuntimeSteeringPlugin`` and ``PausedToolCalls`` see ``F`` and the original
  ``toolUseId`` across approval interrupts and resumes;
- ``AccessDecisionHook`` runs last and decides ``F`` (never ``call_tool``),
  binding one-time grants to ``call_operation_id`` (which matches the original
  ``toolUseId`` in the assistant message);
- ``ChangesFirstToolExecutor`` uses ``resolve_tool_use`` to schedule
  record-changing calls made through ``call_tool`` first;
- stream and history readers (activity phase callbacks, checkpoint-only
  passes, turn work notes, tool-result artifact naming, and tool-call limit
  tracking) use ``unwrap_tool_use`` so they observe ``F`` instead of
  ``call_tool``.

Callable target rule: ``call_tool`` resolves only names in ``catalog.functions``
or ``catalog.function_dispatchers`` (when the dispatcher is already in the
agent's registry). It never searches the registry for non-catalog tools (such
as ``skills``, MCP tools, or structured-output turn-decision tools) and cancels
those without renaming so neither access checks nor structured-output failure
counts are affected.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from strands.hooks import (
    AfterInvocationEvent,
    AfterToolCallEvent,
    BeforeToolCallEvent,
    HookProvider,
    HookRegistry,
)

from custom_agents.runtime_guardrails import STRUCTURED_OUTPUT_TOOL_NAMES
from custom_agents.tool_loading import offered
from custom_agents.tool_presentation import CALL_TOOL_NAME, index_enabled, withheld_functions
from custom_tools.provider_contract import tool_name

logger = logging.getLogger(__name__)

# Catalog tools whose functions cannot be called through call_tool (no recursion into tool loading).
CALL_TOOL_EXCLUDED_TOOLS = frozenset({"fn:tool_loading"})
# Catalog tools owned by Strands plugins rather than providers; always called directly, never through call_tool.
CALL_TOOL_PLUGIN_TOOLS = frozenset({"fn:skills"})
# Strictly before Strands' HookOrder.SDK_FIRST (-100), OnRequestToolHook (-10), steering (0), and AccessDecisionHook (+inf).
CALL_TOOL_HOOK_ORDER = -200.0

_JSON_DECODER = json.JSONDecoder()


@dataclass(frozen=True)
class ResolvedCall:
    """The real function call behind a model tool use (unwrapped if it came through ``call_tool``)."""

    name: str
    inputs: dict[str, Any]
    unwrapped: bool
    error: str | None = None
    inputs_parsed_from_json: bool = False


@dataclass(frozen=True)
class UnwrappedToolUse:
    """Best-effort view of a history or streamed tool use (never raises on partial or malformed data)."""

    name: str | None
    inputs: Any
    unwrapped: bool
    complete: bool


def _parse_inner_inputs(target_name: str, raw_inner: Any) -> tuple[dict[str, Any], str | None, bool]:
    """Validate ``inputs`` of a ``call_tool`` call: mapping, None (empty), or JSON string decoding to a mapping."""

    if raw_inner is None:
        return {}, None, False
    if isinstance(raw_inner, Mapping):
        return dict(raw_inner), None, False
    if isinstance(raw_inner, str):
        try:
            parsed = json.loads(raw_inner)
        except (TypeError, ValueError):
            parsed = None
        if isinstance(parsed, Mapping):
            return dict(parsed), None, True
    return (
        {},
        f"call_tool 'inputs' for {target_name} must be an object (dictionary) of named arguments.",
        False,
    )


def resolve_tool_use(tool_use: Any) -> ResolvedCall:
    """Resolve what function a tool use really calls; total over any input type and never raises."""

    if not isinstance(tool_use, Mapping):
        return ResolvedCall(name="", inputs={}, unwrapped=False, error="Malformed tool call.")
    raw_name = tool_use.get("name")
    outer_name = raw_name.strip() if isinstance(raw_name, str) else ""
    raw_input = tool_use.get("input")
    if outer_name != CALL_TOOL_NAME:
        inputs = dict(raw_input) if isinstance(raw_input, Mapping) else {}
        return ResolvedCall(name=outer_name, inputs=inputs, unwrapped=False)
    if not isinstance(raw_input, Mapping):
        return ResolvedCall(
            name="",
            inputs={},
            unwrapped=True,
            error="call_tool requires an object input with 'name' and optional 'inputs'.",
        )
    raw_target = raw_input.get("name")
    if not isinstance(raw_target, str) or not raw_target.strip():
        return ResolvedCall(
            name="",
            inputs={},
            unwrapped=True,
            error="call_tool requires a non-empty string 'name' naming the tool function to call.",
        )
    target_name = raw_target.strip()
    inner_inputs, error, parsed_from_json = _parse_inner_inputs(target_name, raw_input.get("inputs"))
    return ResolvedCall(
        name=target_name,
        inputs=inner_inputs,
        unwrapped=True,
        error=error,
        inputs_parsed_from_json=parsed_from_json,
    )


def _skip_whitespace(text: str, pos: int) -> int:
    length = len(text)
    while pos < length and text[pos] in " \t\r\n":
        pos += 1
    return pos


def _decode_json_at(text: str, pos: int) -> tuple[Any, int] | None:
    pos = _skip_whitespace(text, pos)
    if pos >= len(text):
        return None
    try:
        return _JSON_DECODER.raw_decode(text, pos)
    except ValueError:
        return None


def _expect_char(text: str, pos: int, ch: str) -> int | None:
    pos = _skip_whitespace(text, pos)
    return pos + 1 if pos < len(text) and text[pos] == ch else None


def _extract_top_level_json_string_field(text: str, field_name: str) -> str | None:
    """Read one complete top-level string field from a possibly partial JSON object without matching nested keys."""

    pos = _expect_char(text, 0, "{")
    while pos is not None:
        key_pair = _decode_json_at(text, pos)
        if key_pair is None or not isinstance(key_pair[0], str):
            return None
        after_colon = _expect_char(text, key_pair[1], ":")
        value_pair = _decode_json_at(text, after_colon) if after_colon is not None else None
        if value_pair is None:
            return None
        if key_pair[0] == field_name:
            return value_pair[0].strip() or None if isinstance(value_pair[0], str) else None
        pos = _expect_char(text, value_pair[1], ",")
    return None


def _unwrap_mapping_input(raw_input: Mapping[str, Any]) -> UnwrappedToolUse:
    resolved = resolve_tool_use({"name": CALL_TOOL_NAME, "input": raw_input})
    if resolved.name:
        inputs = raw_input.get("inputs") if resolved.error else resolved.inputs
        return UnwrappedToolUse(name=resolved.name, inputs=inputs, unwrapped=True, complete=True)
    return UnwrappedToolUse(name=None, inputs=None, unwrapped=True, complete=True)


def unwrap_tool_use(tool_use: Any) -> UnwrappedToolUse:
    """Read the real function name and inputs from history or streaming ``tool_use`` data; never raises."""

    if not isinstance(tool_use, Mapping):
        return UnwrappedToolUse(name=None, inputs=None, unwrapped=False, complete=True)
    raw_name = tool_use.get("name")
    outer_name = raw_name.strip() if isinstance(raw_name, str) else ""
    if not outer_name:
        return UnwrappedToolUse(name=None, inputs=None, unwrapped=False, complete=True)
    raw_input = tool_use.get("input")
    if outer_name != CALL_TOOL_NAME:
        return UnwrappedToolUse(name=outer_name, inputs=raw_input, unwrapped=False, complete=True)
    if isinstance(raw_input, Mapping):
        return _unwrap_mapping_input(raw_input)
    if isinstance(raw_input, str):
        try:
            parsed = json.loads(raw_input)
        except (TypeError, ValueError):
            partial_name = _extract_top_level_json_string_field(raw_input, "name")
            return UnwrappedToolUse(name=partial_name, inputs=None, unwrapped=True, complete=False)
        if isinstance(parsed, Mapping):
            return _unwrap_mapping_input(parsed)
        return UnwrappedToolUse(name=None, inputs=None, unwrapped=True, complete=True)
    return UnwrappedToolUse(name=None, inputs=None, unwrapped=True, complete=True)


# -- callable target resolution -------------------------------------------------------------


def _registry_lookup(registry: Any, name: str) -> Any | None:
    if registry is None:
        return None
    tool = registry.registry.get(name)
    if tool is not None:
        return tool
    return registry.dynamic_tools.get(name)


def _is_mcp_tool(catalog: Any, registry: Any, name: str) -> bool:
    """Whether ``name`` is an MCP server tool (by catalog prefix or registered MCPAgentTool)."""

    for prefix in getattr(catalog, "mcp_prefixes", ()) or ():
        if name.startswith(f"{prefix}_"):
            return True
    reg_tool = _registry_lookup(registry, name)
    if reg_tool is None:
        return False
    if getattr(reg_tool, "tool_type", None) == "mcp" or type(reg_tool).__name__ == "MCPAgentTool":
        return True
    try:
        from strands.tools.mcp.mcp_agent_tool import MCPAgentTool

        return isinstance(reg_tool, MCPAgentTool)
    except ImportError:  # pragma: no cover - strands MCP always installed in runtime
        return False


def _is_callable_target(catalog: Any, name: str) -> bool:
    """The target set ``call_tool`` may resolve: catalog provider functions and catalog dispatchers.

    Plugin-registered tools (``CALL_TOOL_PLUGIN_TOOLS``, such as ``skills``)
    stay outside this set so they are called directly rather than through
    ``call_tool``.
    """

    if name in catalog.function_dispatchers:
        return True
    binding = catalog.functions.get(name)
    return binding is not None and binding.tool_id not in CALL_TOOL_PLUGIN_TOOLS


def _catalog_function_tool(agent: Any, name: str) -> Any | None:
    """The Strands tool for a catalog function: trusted tools with auth on, definition tools with auth off."""

    from custom_agents.access_runtime import authorization_enabled

    if authorization_enabled(getattr(agent, "authorization_config", None)):
        from custom_tools.tool_lookup import trusted_tools

        return trusted_tools().get(name)
    return next((item for item in getattr(agent, "tools", ()) or () if tool_name(item) == name), None)


def _resolve_target_tool(agent: Any, catalog: Any, registry: Any, name: str) -> tuple[Any | None, bool]:
    """Resolve a target in the callable target set: ``(selected_tool, was_in_registry)``.

    Never searches the Strands registry outside ``catalog.functions`` and
    ``catalog.function_dispatchers`` so non-catalog tools (skills, MCP tools,
    structured-output tools) can never be selected through ``call_tool``.
    """

    if name in catalog.function_dispatchers:
        reg_tool = _registry_lookup(registry, name)
        return reg_tool, reg_tool is not None
    if name not in catalog.functions:
        return None, False
    if name in getattr(catalog, "not_offered", ()) or name in withheld_functions(agent, catalog):
        return None, False
    reg_tool = _registry_lookup(registry, name)
    if reg_tool is not None:
        return (reg_tool, True) if offered(agent, [reg_tool]) else (None, True)
    tool = _catalog_function_tool(agent, name)
    if tool is None or not offered(agent, [tool]):
        return None, False
    return tool, False


def _append_text_block(result: Any, text: str) -> Any:
    if not isinstance(result, Mapping):
        return result
    content = list(result.get("content") or []) if isinstance(result.get("content"), list) else []
    content.append({"text": text})
    updated = dict(result)
    updated["content"] = content
    return updated


class CallToolHook(HookProvider):
    """Rewrite ``call_tool(name, inputs)`` to the real function before any other tool hook runs."""

    name = "call_tool"

    def __init__(self, agent: Any) -> None:
        self.agent = agent
        # Calls routed through call_tool whose definition was not in the registry:
        # toolUseId -> (target_name, selected_tool). Used to append the full
        # signature when the real tool runs and returns an error.
        self._unshown_calls: dict[str, tuple[str, Any]] = {}

    def register_hooks(self, registry: HookRegistry, **_: Any) -> None:
        registry.add_callback(BeforeToolCallEvent, self.before_tool_call, order=CALL_TOOL_HOOK_ORDER)
        registry.add_callback(AfterToolCallEvent, self.after_tool_call)
        registry.add_callback(AfterInvocationEvent, self.after_invocation)

    def before_tool_call(self, event: BeforeToolCallEvent) -> None:
        if not isinstance(event.tool_use, Mapping) or event.tool_use.get("name") != CALL_TOOL_NAME:
            return
        tool_use_id = str(event.tool_use.get("toolUseId") or "")
        target_hint = ""
        try:
            raw_input = event.tool_use.get("input")
            if isinstance(raw_input, Mapping) and isinstance(raw_input.get("name"), str):
                target_hint = raw_input["name"].strip()
            self._rewrite(event, tool_use_id)
        except Exception:  # noqa: BLE001 - one broken call must return an error result, never fail the turn
            logger.exception(
                "call_tool could not resolve its target; refusing the call. "
                "event=call_tool_failed agent_id=%s target=%s tool_use_id=%s",
                getattr(self.agent, "id", None),
                target_hint or None,
                tool_use_id or None,
            )
            event.cancel_tool = (
                f"call_tool failed while preparing {target_hint or 'the requested tool'}. "
                "Try again or inspect the tool with describe_tools."
            )

    def _rewrite(self, event: BeforeToolCallEvent, tool_use_id: str) -> None:
        from custom_agents.access_runtime import runtime_access

        agent_id = getattr(self.agent, "id", None)
        if not index_enabled(self.agent):
            logger.info(
                "call_tool was called while the tool index is off. event=call_tool_disabled agent_id=%s tool_use_id=%s",
                agent_id,
                tool_use_id or None,
            )
            event.cancel_tool = "call_tool is only available when the tool index is enabled; call tools directly."
            return

        resolved = resolve_tool_use(event.tool_use)
        if resolved.error:
            logger.info(
                "call_tool received invalid inputs. event=call_tool_invalid_input agent_id=%s target=%s tool_use_id=%s error=%s",
                agent_id,
                resolved.name or None,
                tool_use_id or None,
                resolved.error,
            )
            event.cancel_tool = resolved.error
            return

        if resolved.inputs_parsed_from_json:
            logger.info(
                "call_tool parsed JSON string inputs into an object. "
                "event=call_tool_inputs_string_parsed agent_id=%s target=%s tool_use_id=%s",
                agent_id,
                resolved.name,
                tool_use_id or None,
            )

        catalog = runtime_access(self.agent).catalog
        registry = getattr(getattr(event, "agent", None), "tool_registry", None)
        target = resolved.name

        if self._cancel_disallowed_target(event, catalog, registry, target, tool_use_id):
            return

        selected_tool, was_in_registry = (
            _resolve_target_tool(self.agent, catalog, registry, target)
            if _is_callable_target(catalog, target)
            else (None, False)
        )
        # Fresh dict so agent.messages keeps the original call_tool block intact for history and interrupt resumes.
        event.tool_use = {
            "toolUseId": event.tool_use.get("toolUseId"),
            "name": target,
            "input": resolved.inputs,
        }
        event.selected_tool = selected_tool
        if selected_tool is not None and not was_in_registry and tool_use_id:
            self._unshown_calls[tool_use_id] = (target, selected_tool)

    def _cancel_disallowed_target(
        self,
        event: BeforeToolCallEvent,
        catalog: Any,
        registry: Any,
        target: str,
        tool_use_id: str,
    ) -> bool:
        agent_id = getattr(self.agent, "id", None)
        binding = catalog.functions.get(target)
        if target == CALL_TOOL_NAME or (binding is not None and binding.tool_id in CALL_TOOL_EXCLUDED_TOOLS):
            logger.info(
                "call_tool refused an excluded target. event=call_tool_excluded_target agent_id=%s target=%s tool_use_id=%s",
                agent_id,
                target,
                tool_use_id or None,
            )
            event.cancel_tool = f"{target} cannot be called through call_tool; call {target} directly."
            return True

        if _is_mcp_tool(catalog, registry, target):
            logger.info(
                "call_tool refused an MCP server tool. event=call_tool_mcp_refused agent_id=%s target=%s tool_use_id=%s",
                agent_id,
                target,
                tool_use_id or None,
            )
            event.cancel_tool = (
                f"{target} is an MCP server tool and cannot be called through call_tool. "
                f"Load its server with use_tools if needed and call {target} directly."
            )
            return True

        if not _is_callable_target(catalog, target):
            in_registry = _registry_lookup(registry, target) is not None
            if in_registry or target in STRUCTURED_OUTPUT_TOOL_NAMES:
                logger.info(
                    "call_tool refused a non-catalog tool. event=call_tool_not_catalog_function "
                    "agent_id=%s target=%s tool_use_id=%s",
                    agent_id,
                    target,
                    tool_use_id or None,
                )
                if in_registry:
                    event.cancel_tool = f"{target} is already in your tool list; call it directly, not through call_tool."
                else:
                    event.cancel_tool = (
                        f"{target} is your turn-decision tool, not a catalog function; "
                        "call it directly, not through call_tool."
                    )
                return True
        return False

    def after_tool_call(self, event: AfterToolCallEvent) -> None:
        tool_use_id = str(event.tool_use.get("toolUseId") or "") if isinstance(event.tool_use, Mapping) else ""
        tracked = self._unshown_calls.pop(tool_use_id, None) if tool_use_id else None
        if tracked is None:
            return
        if event.cancel_message is not None or event.selected_tool is None:
            return
        result = event.result
        if not isinstance(result, Mapping) or result.get("status") != "error":
            return
        target_name, tool = tracked
        try:
            from custom_agents.tool_index import signature

            full_sig = signature(target_name, tool)
            event.result = _append_text_block(
                result,
                f"Full signature for {target_name}: {full_sig}",
            )
        except Exception:  # noqa: BLE001 - signature hint is best effort; keep the tool's own error result
            logger.exception(
                "Could not append full signature to a failed call_tool result. "
                "event=call_tool_signature_hint_failed agent_id=%s target=%s tool_use_id=%s",
                getattr(self.agent, "id", None),
                target_name,
                tool_use_id,
            )

    def after_invocation(self, event: AfterInvocationEvent) -> None:
        del event
        self._unshown_calls.clear()


__all__ = [
    "CALL_TOOL_EXCLUDED_TOOLS",
    "CALL_TOOL_HOOK_ORDER",
    "CALL_TOOL_NAME",
    "CallToolHook",
    "ResolvedCall",
    "UnwrappedToolUse",
    "resolve_tool_use",
    "unwrap_tool_use",
]
