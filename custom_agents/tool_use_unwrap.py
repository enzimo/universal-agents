"""Pure helpers for unwrapping ``call_tool(name, inputs)`` tool uses.

Kept in a leaf module (importing only the stdlib and ``CALL_TOOL_NAME``) so
executors, stream callbacks, turn work notes, context filters and tool-call
limit trackers can import it at module top without pulling in Strands hooks,
runtime guardrails or tool loading.

Security invariant: a model's direct tool name is bounded by the model API,
whereas ``call_tool``'s ``name`` argument is an ordinary string. Every target
name is checked once here against ``is_valid_tool_name`` (ASCII letters,
digits, underscore, hyphen, dot and colon, at most ``MAX_TOOL_NAME_CHARS``).
Any invalid name is rejected without repeating the untrusted text in errors,
and ``unwrap_tool_use`` returns ``name=None`` so every stream, note, and log
reader falls back to ``call_tool``.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from custom_agents.tool_presentation import CALL_TOOL_NAME

MAX_TOOL_NAME_CHARS = 128
_VALID_TOOL_NAME = re.compile(rf"[A-Za-z0-9_.:-]{{1,{MAX_TOOL_NAME_CHARS}}}")
_JSON_DECODER = json.JSONDecoder()


def is_valid_tool_name(name: str) -> bool:
    """Whether ``name`` has the safe shape of a tool function name (single line, bounded length)."""

    return bool(name and _VALID_TOOL_NAME.fullmatch(name))


def checked_target_name(tool_use: Any) -> str:
    """Return the validated ``call_tool`` target name, or empty when absent or invalid."""

    if not isinstance(tool_use, Mapping):
        return ""
    raw_input = tool_use.get("input")
    if not isinstance(raw_input, Mapping):
        return ""
    raw_target = raw_input.get("name")
    if not isinstance(raw_target, str):
        return ""
    candidate = raw_target.strip(" \t")
    return candidate if is_valid_tool_name(candidate) else ""


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
    if not isinstance(raw_target, str) or not raw_target.strip(" \t"):
        return ResolvedCall(
            name="",
            inputs={},
            unwrapped=True,
            error="call_tool requires a non-empty string 'name' naming the tool function to call.",
        )
    target_name = raw_target.strip(" \t")
    if not is_valid_tool_name(target_name):
        return ResolvedCall(
            name="",
            inputs={},
            unwrapped=True,
            error="call_tool 'name' must be a tool function name.",
        )
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
            return value_pair[0].strip(" \t") or None if isinstance(value_pair[0], str) else None
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
            if partial_name is not None and not is_valid_tool_name(partial_name):
                partial_name = None
            return UnwrappedToolUse(name=partial_name, inputs=None, unwrapped=True, complete=False)
        if isinstance(parsed, Mapping):
            return _unwrap_mapping_input(parsed)
        return UnwrappedToolUse(name=None, inputs=None, unwrapped=True, complete=True)
    return UnwrappedToolUse(name=None, inputs=None, unwrapped=True, complete=True)


__all__ = [
    "CALL_TOOL_NAME",
    "MAX_TOOL_NAME_CHARS",
    "ResolvedCall",
    "UnwrappedToolUse",
    "checked_target_name",
    "is_valid_tool_name",
    "resolve_tool_use",
    "unwrap_tool_use",
]
