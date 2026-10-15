"""Model choice: TeamLead switches an agent's model without a review each time.

Owner's decision (2026-09-27): "I want the TeamLead to be able to apply model
changes to itself and also to any other agent type without me having to
approve every time." Runtime settings stay staged, digest-checked plans
applied with ``apply_change`` (``access_control.changes``); this module only
names the narrow class of plans that may apply at once and words what happened.

- **The class.** A plan whose every change is one of ``MODEL_CHOICE_PATHS``
  for the current agent or one named agent type (``current_agent.llm.*`` with
  target ``self`` or ``agent_type``). Such a plan is described as the change
  kind ``model_choice`` (catalog ``fn:model_choice``) instead of
  ``runtime_settings``; anything else in the same plan keeps the whole plan a
  ``runtime_settings`` change, which a person approves in the Inbox.
- **The context window belongs to the model.** A switch resets the old
  model's window (``model_runtime_profile.stale_model_fields``), so the
  switch sets the new model's window from the capability lookup (owner's
  decision 2026-10-10). Without one the runtime assumes a fallback window
  (``strands_model_factory.resolve_context_window_tokens``) that can be far
  too small: a 1M-token model judged against 128K hid every fresh tool
  result.
- **Left out on purpose.** Team-wide defaults (``workspace_default.llm.*``,
  ``llm.*``) change every agent at once; temperature and output limits are
  tuning, not the choice of model; and
  ``litellm_api_mode`` switches the request API and its endpoint (Chat
  Completions or Responses, which needs a Responses-capable endpoint and
  streaming, ``custom_agents.strands_model_factory``), so it changes the
  endpoint contract. The endpoint itself (``llm.litellm_api_base``) is a
  team-wide setting an administrator approves in the Inbox
  (``custom_agents.endpoint_switch``), and keys are saved by administrators
  with ``/auth model``, never by agents.
- **Authority.** ``apply_change`` applies a ``model_choice`` plan at once
  only when the access service says TeamLead holds ``fn:model_choice`` for
  the person its task works for and that person holds it too
  (``AccessRequests.submit_change``, approval mode ``held_access``). Only
  administrators hold it (catalog ``owner_scope: administrators``, Cedar
  ``guard:admin-only-tools``); the Trusted starter profile gives it to
  TeamLead for an administrator's work. A block on it sends the change to the
  Inbox again.
- **Usable providers.** With authorization on, a provider change must name a
  provider this instance is already set up for (``usable_providers``):
  endpoints and credentials are the operator's, so a provider nobody set up
  cannot work, and the preflight model call would only fail after the change
  was asked for. With authorization off nothing changes: the preflight call
  still tests the candidate before anything is written.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Iterable, Mapping, Protocol, Sequence

import yaml

from custom_agents.runtime_setting_registry import SettingTargetScope, next_turn_note

logger = logging.getLogger(__name__)

MODEL_CHOICE_CHANGE_KIND = "model_choice"
MODEL_CHOICE_TOOL_ID = "fn:model_choice"
PROVIDER_PATH = "current_agent.llm.provider"
MODEL_ID_PATH = "current_agent.llm.model_id"
CONTEXT_WINDOW_PATH = "current_agent.llm.context_window_tokens"
MODEL_CHOICE_PATHS = frozenset(
    {
        PROVIDER_PATH,
        MODEL_ID_PATH,
        "current_agent.llm.reasoning_effort",
        "current_agent.llm.reasoning_summary",
        CONTEXT_WINDOW_PATH,
        "current_agent.llm.supports_image_input",
        "current_agent.llm.supports_audio_input",
        "current_agent.llm.supports_file_input",
    }
)
_AGENT_SCOPES = frozenset({SettingTargetScope.SELF, SettingTargetScope.AGENT_TYPE})
# How each setting reads in the chat message and the notice.
_INPUT_WORDS = {
    "current_agent.llm.supports_image_input": "images",
    "current_agent.llm.supports_audio_input": "audio",
    "current_agent.llm.supports_file_input": "files",
}


class PlannedChange(Protocol):
    """One change of a staged runtime settings plan (``RuntimeSettingPlanChange``)."""

    path: str
    value: Any
    previous_value: Any
    target_scope: SettingTargetScope
    target_agent_type: str | None


def is_model_choice(changes: Sequence[PlannedChange]) -> bool:
    """Whether a plan changes only model-choice settings of agents (never team defaults)."""

    return bool(changes) and all(
        change.path in MODEL_CHOICE_PATHS and change.target_scope in _AGENT_SCOPES for change in changes
    )


# -- usable providers -------------------------------------------------------------


def _normalized(provider: Any) -> str:
    from custom_agents.strands_model_factory import normalize_model_provider

    return normalize_model_provider(provider) if str(provider or "").strip() else ""


def _llm_providers(llm: Any) -> Iterable[Any]:
    """Providers one LLM section names: its own, compaction's, and its failover fallbacks."""

    if llm is None:
        return
    read = llm.get if isinstance(llm, Mapping) else lambda name: getattr(llm, name, None)
    yield read("provider")
    yield read("model_provider")
    yield read("compaction_model_provider")
    failover = read("failover")
    fallbacks = failover.get("fallbacks") if isinstance(failover, Mapping) else None
    for fallback in fallbacks or ():
        if isinstance(fallback, Mapping):
            yield fallback.get("provider") or fallback.get("model_provider")


def _override_entries(overrides_path: Path) -> Iterable[Any]:
    try:
        payload = yaml.safe_load(overrides_path.read_text(encoding="utf-8")) if overrides_path.is_file() else {}
    except (OSError, yaml.YAMLError):
        logger.warning(
            "Could not read the LLM override file for usable providers. event=model_choice_overrides_unreadable path=%s",
            overrides_path,
        )
        return
    if not isinstance(payload, Mapping):
        return
    yield payload.get("default")
    agent_types = payload.get("agent_types")
    if isinstance(agent_types, Mapping):
        yield from agent_types.values()


def usable_providers(agent: Any, config: Any, overrides_path: Path) -> frozenset[str]:
    """The model providers this instance is already set up for.

    Evidence, each a provider the runtime already calls with the operator's
    endpoint and credentials: the running agent's own provider (it includes
    environment overrides such as ``LLM_PROVIDER``); the instance
    configuration's team model, compaction model and failover fallbacks, and
    the same for every agent type; and every provider the LLM override file
    already sets (each written after a successful test call).
    """

    sources: list[Any] = [getattr(agent, "model_provider", None), *_llm_providers(getattr(config, "llm", None))]
    for settings in (getattr(config, "agent_types", None) or {}).values():
        sources.extend(_llm_providers(getattr(settings, "llm", None)))
    for entry in _override_entries(overrides_path):
        if isinstance(entry, Mapping):
            sources.extend(_llm_providers(entry))
    return frozenset(name for name in map(_normalized, sources) if name)


def refuse_unusable_provider(changes: Sequence[PlannedChange], usable: frozenset[str]) -> None:
    """Raise a plain ValueError when a provider change names a provider nobody set up."""

    for change in changes:
        if change.path != PROVIDER_PATH or change.value in (None, ""):
            continue
        if _normalized(change.value) in usable:
            continue
        logger.info(
            "Refused a provider this instance is not set up for. event=model_choice_provider_refused "
            "provider=%s target_agent_type=%s usable=%s",
            change.value,
            change.target_agent_type,
            ",".join(sorted(usable)),
        )
        raise ValueError(
            f"{change.value} is not a model provider this instance is set up for "
            f"(set up here: {', '.join(sorted(usable)) or 'none'}). Keep the provider and change the model, "
            "or ask an administrator to set the provider up in the instance configuration first."
        )


# -- what happened, in plain words --------------------------------------------------


def _shown(value: Any) -> str:
    if value is None or value == "":
        return "not set"
    if value is True:
        return "on"
    if value is False:
        return "off"
    return str(value)


def _by_agent_type(changes: Sequence[PlannedChange]) -> dict[str, list[PlannedChange]]:
    grouped: dict[str, list[PlannedChange]] = {}
    for change in changes:
        grouped.setdefault(str(change.target_agent_type or "this agent"), []).append(change)
    return grouped


def _setting_words(change: PlannedChange, value: Any) -> str:
    if change.path in _INPUT_WORDS:
        return f"{_INPUT_WORDS[change.path]} {_shown(value)}"
    if change.path == CONTEXT_WINDOW_PATH:
        return f"context window {value:,} tokens" if isinstance(value, int) else f"context window {_shown(value)}"
    name = change.path.removeprefix("current_agent.llm.").replace("_", " ")
    return f"{name} {_shown(value)}"


def _values(changes: Sequence[PlannedChange], *, previous: bool) -> dict[str, Any]:
    return {change.path: change.previous_value if previous else change.value for change in changes}


def _model_words(values: Mapping[str, Any]) -> str:
    """"deepseek/deepseek-v4.1-flash" or "deepseek/deepseek-v4.1-flash on litellm"."""

    model = values.get(MODEL_ID_PATH)
    provider = values.get(PROVIDER_PATH)
    if model and provider:
        return f"{model} on {provider}"
    return str(model or f"provider {provider}")


def _headline(agent_type: str, changes: Sequence[PlannedChange]) -> str:
    """"Switched X to model (images on, files off)", without the closing period."""

    new = _values(changes, previous=False)
    others = [
        _setting_words(change, change.value) for change in changes if change.path not in {MODEL_ID_PATH, PROVIDER_PATH}
    ]
    if MODEL_ID_PATH in new or PROVIDER_PATH in new:
        detail = f" ({', '.join(others)})" if others else ""
        return f"Switched {agent_type} to {_model_words(new)}{detail}"
    return f"Changed {agent_type}'s model settings: {', '.join(others)}"


def _agent_line(agent_type: str, changes: Sequence[PlannedChange], actor_type: str) -> str:
    # Model choices are stored in LLM_OVERRIDES.yaml, which every agent reads again
    # at its next turn (runtime_setting_registry.next_turn_targets); none needs a restart.
    own = agent_type in {actor_type, "this agent"}
    timing = "It takes effect now." if own else next_turn_note([f"{agent_type} agents"])
    return f"{_headline(agent_type, changes)}. {timing}"


def applied_message(changes: Sequence[PlannedChange], actor_type: str) -> str:
    """The short chat message after ``actor_type`` applied a model choice, one sentence pair per agent type."""

    return " ".join(
        _agent_line(agent_type, group, actor_type) for agent_type, group in _by_agent_type(changes).items()
    )


def _switch_back(agent_type: str, changes: Sequence[PlannedChange]) -> str:
    before = _values(changes, previous=True)
    parts = [
        f"{'model' if change.path == MODEL_ID_PATH else 'provider'} {_shown(before[change.path])}"
        if change.path in {MODEL_ID_PATH, PROVIDER_PATH}
        else _setting_words(change, change.previous_value)
        for change in changes
    ]
    shown = ", ".join(parts)
    return f"Before, {agent_type} had {shown}. To switch back, ask TeamLead to switch {agent_type} back to {shown}."


def notice_text(actor_type: str, changes: Sequence[PlannedChange]) -> tuple[str, str]:
    """The Inbox notice's title and summary: what changed, the previous values, how to switch back."""

    grouped = _by_agent_type(changes)
    title = "; ".join(_headline(agent_type, group) for agent_type, group in grouped.items())
    summary = " ".join(
        [
            f"{actor_type} used the model choice access you hold, so nobody was asked.",
            applied_message(changes, actor_type),
            *(_switch_back(agent_type, group) for agent_type, group in grouped.items()),
            "To be asked each time again, block TeamLead's Model choice on the Access page.",
        ]
    )
    return f"{actor_type}: {title}", summary


def tell_person(agent: Any, *, person_id: str | None, actor_type: str, changes: Sequence[PlannedChange]) -> None:
    """Send the person the work was for an Inbox notice (nothing with authorization off)."""

    from custom_agents.access_runtime import send_access_notice
    from custom_messages.contracts.access_v2 import AccessNoticeKind

    if not person_id:
        logger.warning(
            "A model choice applied for no person, so no notice was sent. event=model_choice_notice_skipped "
            "actor=%s",
            actor_type,
        )
        return
    title, summary = notice_text(actor_type, changes)
    send_access_notice(agent, kind=AccessNoticeKind.MODEL_CHANGED, title=title, summary=summary, person_id=person_id)


__all__ = [
    "MODEL_CHOICE_CHANGE_KIND",
    "MODEL_CHOICE_PATHS",
    "MODEL_CHOICE_TOOL_ID",
    "applied_message",
    "is_model_choice",
    "notice_text",
    "refuse_unusable_provider",
    "tell_person",
    "usable_providers",
]
