"""The one-line index of an agent's tools, and the exact inputs ``describe_tools`` shows.

Why: full tool definitions cost about 250 tokens each on every model call and
crowd the model's choice; a search function alone can miss a tool, and then
the model never learns it exists. With ``agent.tool_presentation`` set to
``index`` or ``index_and_scripts`` (``custom_agents.tool_presentation``), only
the core tools keep full definitions, and every other tool the agent holds is
listed here in one line: its name and the first sentence of its own
description. The list is complete for what the agent holds, so nothing it
holds depends on search to be found.

How it works:

- **What is listed.** The functions the agent holds now (with authorization on
  the grants the tool-list sync computed for the task's person, with it off
  the definition's tools) that are not shown in full. Loading a group does not
  change the index, so the system prompt stays the same while the agent loads
  and unloads groups; it changes only when what the agent holds changes.
- **Where it goes.** ``ToolIndexHook`` runs before every model call, after the
  tool-list sync, and appends the index to the Strands agent's system prompt
  when it changed. Strands reads the system prompt after these hooks.
- **Using a listed function.** ``use_tools`` loads its group, a call by name
  loads it too (``custom_agents.tool_loading.OnRequestToolHook``), and
  ``describe_tools`` returns the exact inputs (``signature``). With tool
  scripts, functions marked ``script`` run from ``run_tool_script`` without
  loading (``script_inputs``). Nothing here gives access: the access hook
  decides every call.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any

from strands.hooks import BeforeModelCallEvent, HookProvider, HookRegistry

from custom_messages.contracts.access_v2 import AccessLevel

logger = logging.getLogger(__name__)

# After the tool-list sync (-100) has recorded what the agent holds, before the person's model (-50).
TOOL_INDEX_HOOK_ORDER = -90.0
# The longest first sentence an index line may carry (tests/test_tool_index.py checks every tool).
MAX_INDEX_SENTENCE_CHARS = 160
# Catalog tools whose functions never run from a tool script: loading and finding
# tools, the script tool itself, and model-context loading, which only a direct
# call can hand to the model.
SCRIPT_EXCLUDED_TOOLS = frozenset({"fn:tool_loading", "fn:tool_scripts"})
SCRIPT_EXCLUDED_FUNCTIONS = frozenset({"load_local_media_for_model_context"})

_SENTENCE_END = re.compile(r"(.+?[.!?])(?:\s|$)")
_JSON_TYPES = {"string": "str", "integer": "int", "number": "float", "boolean": "bool", "object": "dict", "null": "None"}


def first_sentence(description: str) -> str:
    """The first sentence of a tool's description (its first paragraph when it has no sentence end)."""

    paragraph = " ".join(str(description or "").strip().split("\n\n")[0].split())
    match = _SENTENCE_END.match(paragraph)
    return match.group(1) if match else paragraph


@dataclass(frozen=True)
class ScriptInputs:
    """The inputs a tool script may call a function with: any (``argument`` None), or these values of one argument."""

    argument: str | None = None
    values: tuple[str, ...] = ()

    def allows(self, inputs: Mapping[str, Any]) -> bool:
        return self.argument is None or inputs.get(self.argument) in self.values


def script_inputs(catalog: Any, name: str) -> ScriptInputs | None:
    """When a tool script may call this function: the inputs it may use, or None when it may not.

    A script calls only operations the catalog rates ``read``: a function with
    a fixed read level for any inputs, and an action tool only for the
    argument values whose level is read. Dispatchers (``secure_cli``,
    ``invoke_installed_permission``) are not catalog functions and never
    qualify, nor do the excluded tools and functions above.
    """

    binding = catalog.functions.get(name)
    if binding is None or binding.tool_id in SCRIPT_EXCLUDED_TOOLS or name in SCRIPT_EXCLUDED_FUNCTIONS:
        return None
    if binding.level is not None:
        return ScriptInputs() if binding.level is AccessLevel.READ else None
    if not binding.argument:
        return None
    values = tuple(sorted(value for value, level in binding.values.items() if level is AccessLevel.READ))
    return ScriptInputs(binding.argument, values) if values else None


def script_note(catalog: Any, name: str) -> str:
    """How an index line marks a function a script may call: ``script`` or ``script: action=list|read``."""

    allowed = script_inputs(catalog, name)
    if allowed is None:
        return ""
    if allowed.argument is None:
        return "script"
    return f"script: {allowed.argument}={'|'.join(allowed.values)}"


INDEX_HEADER = (
    "Your other tools:",
    "Each line below is a tool function you may use but whose full definition is not in your tool list. "
    "To use one, load its group with use_tools(tool_ids=['<group id>']) (it is in your tool list from your "
    "next step), or call it by name. describe_tools(names=['<function>']) gives its exact inputs first.",
)
INDEX_SCRIPT_NOTE = (
    "Functions marked script also run from run_tool_script without loading: use it to chain several "
    "lookups and return only what you need. A mark like script: action=list|read allows only those values."
)


def _by_group(catalog: Any, names: Iterable[str], tools: Mapping[str, Any]) -> dict[str, list[str]]:
    """Catalog functions among ``names`` that have a tool, by the group ``use_tools`` loads."""

    by_group: dict[str, list[str]] = {}
    for name in sorted(set(names)):
        binding = catalog.functions.get(name)
        if binding is not None and name in tools:
            by_group.setdefault(binding.tool_id, []).append(name)
    return by_group


def _group_lines(catalog: Any, tool_id: str, names: list[str], tools: Mapping[str, Any], *, scripts: bool) -> list[str]:
    tool = catalog.tools.get(tool_id)
    lines = [f"{tool_id} ({getattr(tool, 'display_name', '') or tool_id})"]
    for name in names:
        note = script_note(catalog, name) if scripts else ""
        marker = f" [{note}]" if note else ""
        lines.append(f"- {name}{marker}: {first_sentence(_description(tools[name]))}")
    return lines


def index_text(catalog: Any, names: Iterable[str], tools: Mapping[str, Any], *, scripts: bool) -> str:
    """The prompt's index of the functions in ``names`` (tool name -> Strands tool in ``tools``), or empty."""

    by_group = _by_group(catalog, names, tools)
    if not by_group:
        return ""
    lines = [*INDEX_HEADER, *((INDEX_SCRIPT_NOTE,) if scripts else ())]
    for tool_id in sorted(by_group):
        lines.extend(_group_lines(catalog, tool_id, by_group[tool_id], tools, scripts=scripts))
    return "\n".join(lines)


def _description(tool: Any) -> str:
    spec = getattr(tool, "tool_spec", None) or {}
    return str(spec.get("description") or "")


def _schema_of(tool: Any) -> Mapping[str, Any]:
    spec = getattr(tool, "tool_spec", None) or {}
    schema = spec.get("inputSchema") or {}
    return schema.get("json", schema) if isinstance(schema, Mapping) else {}


def _type_of(schema: Mapping[str, Any]) -> str:
    """A Python-style type for one JSON schema (best effort; the description carries the details)."""

    if "enum" in schema:
        return "Literal[" + ", ".join(repr(value) for value in schema["enum"]) + "]"
    for key in ("anyOf", "oneOf"):
        if key in schema:
            parts = [_type_of(option) for option in schema[key] if isinstance(option, Mapping)]
            return " | ".join(dict.fromkeys(parts)) or "Any"
    kind = schema.get("type")
    if isinstance(kind, list):
        return " | ".join(_JSON_TYPES.get(str(item), "Any") for item in kind)
    if kind == "array":
        items = schema.get("items")
        return f"list[{_type_of(items) if isinstance(items, Mapping) else 'Any'}]"
    return _JSON_TYPES.get(str(kind), "Any")


def signature(name: str, tool: Any) -> str:
    """A Python-style signature from the tool's input schema: required inputs first, then optional ones."""

    schema = _schema_of(tool)
    properties: Mapping[str, Any] = schema.get("properties") or {}
    required = set(schema.get("required") or ())
    first: list[str] = []
    rest: list[str] = []
    for argument, prop in properties.items():
        prop = prop if isinstance(prop, Mapping) else {}
        text = f"{argument}: {_type_of(prop)}"
        if argument in required:
            first.append(text)
        else:
            rest.append(f"{text} = {prop.get('default')!r}")
    return f"{name}({', '.join(first + rest)})"


def described(name: str, tool: Any, catalog: Any, *, scripts: bool) -> dict[str, Any]:
    """What ``describe_tools`` returns for one function."""

    binding = catalog.functions.get(name)
    entry: dict[str, Any] = {
        "name": name,
        "signature": signature(name, tool),
        "description": _description(tool).strip(),
        "group": binding.tool_id if binding is not None else None,
    }
    if scripts:
        note = script_note(catalog, name)
        entry["tool_script"] = note or "no: call it directly"
    return entry


class ToolIndexHook(HookProvider):
    """Before every model call, keep the index in the Strands agent's system prompt equal to what it holds."""

    name = "tool_index"

    def __init__(self, agent: Any, parts: Any, base_prompt: str) -> None:
        self.agent = agent
        self.parts = parts
        self.base_prompt = base_prompt
        self._text: str | None = None

    def register_hooks(self, registry: HookRegistry, **_: Any) -> None:
        registry.add_callback(BeforeModelCallEvent, self.before_model_call, order=TOOL_INDEX_HOOK_ORDER)

    def before_model_call(self, event: BeforeModelCallEvent) -> None:
        try:
            text = self.current_text()
            if text == self._text:
                return
            self._text = text
            event.agent.system_prompt = f"{self.base_prompt}\n\n{text}" if text else self.base_prompt
            logger.info(
                "The agent's tool index changed. event=tool_index_applied agent_id=%s lines=%d",
                getattr(self.agent, "id", None),
                text.count("\n- "),
            )
        except Exception:  # noqa: BLE001 - the prompt keeps its last index; access still decides every call
            logger.exception(
                "Could not update the agent's tool index. event=tool_index_failed agent_id=%s",
                getattr(self.agent, "id", None),
            )

    def current_text(self) -> str:
        from custom_agents.access_runtime import runtime_access
        from custom_agents.tool_loading import offered_names, shown_names
        from custom_agents.tool_presentation import scripts_enabled, withheld_functions
        from custom_tools.tool_lookup import trusted_tools

        catalog = runtime_access(self.agent).catalog
        held = held_functions(self.agent, self.parts)
        # Shown in full before anything loads: the core, the definition's groups, kept-shown functions.
        indexed = (
            offered_names(self.agent, held)
            - shown_names(self.agent, catalog, held, at_start=True)
            - withheld_functions(self.agent, catalog)
        )
        return index_text(catalog, indexed, trusted_tools(), scripts=scripts_enabled(self.agent))


def held_functions(agent: Any, parts: Any) -> set[str]:
    """The catalog functions the agent holds now: the sync's record, else the definition's tools."""

    from custom_tools.provider_contract import tool_name

    sync = getattr(parts, "sync", None)
    recorded = getattr(sync, "held_functions", None)
    if recorded is not None:
        return set(recorded)
    return {tool_name(tool) for tool in getattr(agent, "tools", ()) or ()}


__all__ = [
    "ScriptInputs",
    "MAX_INDEX_SENTENCE_CHARS",
    "SCRIPT_EXCLUDED_FUNCTIONS",
    "SCRIPT_EXCLUDED_TOOLS",
    "TOOL_INDEX_HOOK_ORDER",
    "ToolIndexHook",
    "described",
    "first_sentence",
    "held_functions",
    "index_text",
    "script_inputs",
    "script_note",
    "signature",
]
