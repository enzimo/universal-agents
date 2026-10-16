"""One-line summaries of runtime settings plans (``ChangeProposalV1.summary``).

A person deciding a settings change in chat reads one sentence such as
"Switch TeamArchitect to deepseek/deepseek-v4.1-flash via openrouter (images
on). Takes effect now." This module writes it from the plan's own before and
after values, so the words never come from the agent or from the setting
registry's long descriptions:

- Values that do not change are left out.
- Model settings for one agent type (or a team or workspace default) read as
  one "Switch ... to <model> via <provider>" phrase with the changed input
  kinds in brackets.
- Other settings read "set <name> to <value>" (or "turn <name> on/off");
  long text values are named, not quoted.
- The sentence ends with when the change takes effect, from the plan's
  restart scope (the registry decides it per setting). Model settings that
  other running agents read again at their next turn
  (``runtime_setting_registry.next_turn_targets``) say so instead of "now".

The Inbox keeps the plan's full effect lines; this is only the headline.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable, Protocol

from custom_agents.runtime_setting_registry import NEXT_TURN_TIMING
from custom_messages.contracts.access_v2 import summary_line
from custom_messages.contracts.runtime_v1 import (
    RESTART_SCOPE_ALL,
    RESTART_SCOPE_GATEWAY,
    RESTART_SCOPE_MANAGER,
    RESTART_SCOPE_NONE,
)

CURRENT_AGENT_PREFIX = "current_agent."
WORKSPACE_DEFAULT_PREFIX = "workspace_default."
TEAM_DEFAULT_LLM_PREFIX = "llm."
LLM_SEGMENT = "llm."
PROVIDER_KEY = "provider"
MODEL_KEY = "model_id"
# Input kinds a model accepts, as a person names them.
CAPABILITY_WORDS = {
    "supports_image_input": "images",
    "supports_audio_input": "audio",
    "supports_file_input": "files",
}
# Short names for settings; any other setting is named from its path.
SETTING_WORDS = {
    "context_window_tokens": "context window",
    "reasoning_effort": "reasoning effort",
    "reasoning_summary": "reasoning summaries",
    "litellm_api_mode": "LiteLLM request API",
    "temperature": "temperature",
    "max_output_tokens": "output limit",
    "stream": "streaming",
    "reasoning_trace_retention_messages": "kept reasoning traces",
    "team_lead.soul": "SOUL.md",
    "specialist.display_name": "display name",
    "specialist.role": "role",
    "specialist.description": "description",
    "specialist.prompt": "instructions",
    "heartbeat.enabled": "the heartbeat",
    "heartbeat.interval_seconds": "the heartbeat interval (seconds)",
    "voice.enabled": "voice",
    "delivery.quiet_hours.enabled": "quiet hours",
    "agent.debug.context_dump.enabled": "context dumps",
    "agent.debug.context_dump.mode": "the context dump mode",
    "agent.max_tool_calls_per_task": "the tool call limit per task",
}
# Values longer than this are named ("update SOUL.md"), not quoted.
MAX_QUOTED_VALUE_CHARS = 80
# Past this length the sentence keeps its first phrases and counts the rest.
MAX_PHRASES_CHARS = 220
TIMING_WORDS = {
    RESTART_SCOPE_NONE: "Takes effect now.",
    RESTART_SCOPE_MANAGER: "Takes effect after a manager restart.",
    RESTART_SCOPE_GATEWAY: "Takes effect after a gateway restart.",
    RESTART_SCOPE_ALL: "Takes effect after a full restart.",
}
NOTHING_CHANGES = "Changes nothing: every setting already has this value."


class SettingChange(Protocol):
    """What the summary reads from one planned change (``RuntimeSettingPlanChange``)."""

    path: str
    value: Any
    previous_value: Any
    target_agent_type: str | None


@dataclass
class _Subject:
    """Whose settings change: an agent type, or the team or workspace default model."""

    name: str  # "TeamArchitect", "the team default model"
    owner: str  # "TeamArchitect's", "the team default"
    model: dict[str, Any] = field(default_factory=dict)
    capabilities: list[tuple[str, Any]] = field(default_factory=list)
    others: list[tuple[str, Any]] = field(default_factory=list)


def summarize_runtime_settings(
    changes: Iterable[SettingChange], restart_scope: str, *, next_turn: bool = False
) -> str:
    """The plan in one line; see the module docstring."""

    subjects: dict[str, _Subject] = {}
    for change in changes:
        if change.value == change.previous_value:
            continue
        subject, key = _subject_and_key(change, subjects)
        if key in (PROVIDER_KEY, MODEL_KEY):
            subject.model[key] = change.value
        elif key in CAPABILITY_WORDS:
            subject.capabilities.append((CAPABILITY_WORDS[key], change.value))
        else:
            subject.others.append((key, change.value))
    phrases = [phrase for subject in subjects.values() for phrase in _phrases(subject)]
    if not phrases:
        return NOTHING_CHANGES
    timing = (
        NEXT_TURN_TIMING
        if next_turn and restart_scope == RESTART_SCOPE_NONE
        else TIMING_WORDS.get(restart_scope, TIMING_WORDS[RESTART_SCOPE_ALL])
    )
    return summary_line(f"{_join(phrases)}. {timing}")


def _subject_and_key(change: SettingChange, subjects: dict[str, _Subject]) -> tuple[_Subject, str]:
    path = change.path
    if path.startswith(CURRENT_AGENT_PREFIX):
        name = change.target_agent_type or "this agent"
        subject = subjects.setdefault(name, _Subject(name, f"{name}'s"))
        return subject, _key(path.removeprefix(CURRENT_AGENT_PREFIX))
    if path.startswith(WORKSPACE_DEFAULT_PREFIX):
        subject = subjects.setdefault(
            WORKSPACE_DEFAULT_PREFIX, _Subject("the workspace default model", "the workspace default")
        )
        return subject, _key(path.removeprefix(WORKSPACE_DEFAULT_PREFIX))
    if path.startswith(TEAM_DEFAULT_LLM_PREFIX):
        subject = subjects.setdefault(TEAM_DEFAULT_LLM_PREFIX, _Subject("the team default model", "the team default"))
        return subject, _key(path)
    # Other settings keep their full path as the key; an agent type they
    # target is named after them.
    name = change.target_agent_type or ""
    subject = subjects.setdefault(f"other:{name}", _Subject(name, f"{name}'s" if name else ""))
    return subject, path


def _key(path: str) -> str:
    return path.removeprefix(LLM_SEGMENT)


def _phrases(subject: _Subject) -> list[str]:
    phrases: list[str] = []
    capabilities = ", ".join(f"{word} {_switch(value)}" for word, value in subject.capabilities)
    if subject.model:
        phrase = f"switch {subject.name} to {_model_words(subject.model)}"
        phrases.append(f"{phrase} ({capabilities})" if capabilities else phrase)
    elif capabilities:
        phrases.append(f"turn {_and(capabilities)} for {subject.name}")
    phrases.extend(_setting_phrase(subject, key, value) for key, value in subject.others)
    return phrases


def _model_words(model: dict[str, Any]) -> str:
    provider = model.get(PROVIDER_KEY)
    if MODEL_KEY not in model:
        return f"the {provider or 'default'} provider"
    words = str(model[MODEL_KEY]) if model[MODEL_KEY] else "the default model"
    return f"{words} via {provider}" if PROVIDER_KEY in model and provider else words


def _setting_phrase(subject: _Subject, key: str, value: Any) -> str:
    name = SETTING_WORDS.get(key) or key.replace(".", " ").replace("_", " ")
    if subject.owner and key in SETTING_WORDS:
        name = f"{subject.owner} {name}"  # "TeamArchitect's temperature", "the team default temperature"
    elif subject.name:
        name = f"{name} for {subject.name}"
    if isinstance(value, bool):
        return f"turn {name} {_switch(value)}"
    if value is None:
        return f"reset {name} to the default"
    if isinstance(value, (list, tuple)):
        shown = ", ".join(str(item) for item in value) or "none"
    else:
        shown = str(value)
    if len(shown) > MAX_QUOTED_VALUE_CHARS or "\n" in shown:
        return f"update {name}"
    return f"set {name} to {shown}"


def _switch(value: Any) -> str:
    if value is None:
        return "back to the default"
    return "on" if bool(value) else "off"


def _and(items: str) -> str:
    """Join the last two items with "and": "images on, files off" -> "images on and files off"."""

    head, _sep, last = items.rpartition(", ")
    return f"{head} and {last}" if head else items


def _join(phrases: list[str]) -> str:
    """Phrases as one sentence; a long list keeps its first phrases and counts the rest."""

    kept: list[str] = []
    for phrase in phrases:
        if kept and len("; ".join([*kept, phrase])) > MAX_PHRASES_CHARS:
            break
        kept.append(phrase)
    more = len(phrases) - len(kept)
    text = "; ".join(kept)
    if more:
        text += f"; and {more} more setting change{'s' if more > 1 else ''}"
    return text[:1].upper() + text[1:]


__all__ = ["NOTHING_CHANGES", "SettingChange", "summarize_runtime_settings"]
