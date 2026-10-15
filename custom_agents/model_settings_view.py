"""The model settings an agent type runs with, shown after its model changes.

Owner's request (2026-09-27): "When switching models, I would like the agent
to tell me all of the settings that are set for that model", as a table, and,
for OpenAI models, a suggestion to use the Responses API with reasoning
summaries. Trusted code builds both from the saved settings after a change
applies (``RuntimeSettingsService.apply``), so the person sees what the agent
type will actually run with, not what the agent believes it set:

- ``settings_table`` lists every row of ``MODEL_SETTING_ROWS`` for one agent
  type, read through the same effective-value lookup the plan uses for its
  previous values (override entry, then the running agent, then the instance
  configuration).
- ``responses_suggestion`` names the next step for an OpenAI reasoning model
  reached through LiteLLM's Chat Completions API: the Responses API
  (``litellm_api_mode: responses``) carries OpenAI's reasoning summaries, and
  ``reasoning_summary: auto`` turns them on. A summary without the Responses
  API fails the model's test call (``custom_agents.strands_model_factory``),
  so the two are suggested together. Changing the request API needs an
  administrator's approval (``custom_agents.model_choice``); the suggestion
  says so and changes nothing.
"""

from __future__ import annotations

from typing import Any, Callable, Mapping

# (setting, label) in the order the table shows them.
MODEL_SETTING_ROWS: tuple[tuple[str, str], ...] = (
    ("provider", "Provider"),
    ("model_id", "Model"),
    ("reasoning_effort", "Reasoning effort"),
    ("reasoning_summary", "Reasoning summary"),
    ("litellm_api_mode", "Request API (LiteLLM)"),
    ("context_window_tokens", "Context window (tokens)"),
    ("supports_image_input", "Image input"),
    ("supports_audio_input", "Audio input"),
    ("supports_file_input", "File input"),
)
# What an unset value means for the runtime, per setting.
_UNSET_WORDS = {
    "litellm_api_mode": "chat_completions (default)",
    "context_window_tokens": "automatic",
    "reasoning_summary": "off",
}


def effective_values(read: Callable[[str], Any]) -> dict[str, Any]:
    """Each setting of ``MODEL_SETTING_ROWS`` as ``read`` resolves it for one agent type."""

    return {setting: read(setting) for setting, _label in MODEL_SETTING_ROWS}


def _shown(setting: str, value: Any) -> str:
    if value is None or value == "":
        return _UNSET_WORDS.get(setting, "not set")
    if isinstance(value, bool):
        return "on" if value else "off"
    return str(value).replace("|", "\\|")


def settings_table(agent_type: str, values: Mapping[str, Any]) -> str:
    """A Markdown table of the agent type's model settings."""

    rows = "\n".join(
        f"| {label} | {_shown(setting, values.get(setting))} |" for setting, label in MODEL_SETTING_ROWS
    )
    return f"{agent_type} now runs with:\n\n| Setting | Value |\n|---|---|\n{rows}"


def responses_suggestion(agent_type: str, values: Mapping[str, Any]) -> str | None:
    """Suggest the Responses API and reasoning summaries for an OpenAI reasoning model, or None."""

    from custom_agents.strands_model_factory import is_openai_reasoning_model, normalize_model_provider
    from runtime_settings.model_request_options import (
        LITELLM_API_MODE_RESPONSES,
        normalize_litellm_api_mode,
    )

    provider = str(values.get("provider") or "").strip()
    model_id = str(values.get("model_id") or "")
    if not provider or normalize_model_provider(provider) != "litellm" or not is_openai_reasoning_model(model_id):
        return None
    if normalize_litellm_api_mode(values.get("litellm_api_mode")) != LITELLM_API_MODE_RESPONSES:
        return (
            f"{model_id} is an OpenAI reasoning model. Through the Responses API it also returns "
            "summaries of its reasoning that you can read. To use it, ask TeamLead to set "
            f"{agent_type}'s request API to responses and its reasoning summary to auto. "
            "Changing the request API needs an administrator's approval, and your LiteLLM server "
            "must offer the Responses API."
        )
    if not values.get("reasoning_summary"):
        return (
            f"{model_id} can return summaries of its reasoning. To see them, ask TeamLead to set "
            f"{agent_type}'s reasoning summary to auto."
        )
    return None


def model_settings_report(agent_type: str, read: Callable[[str], Any]) -> dict[str, Any]:
    """The table and any suggestion for one agent type, as the apply result carries them."""

    values = effective_values(read)
    report: dict[str, Any] = {"agent_type": agent_type, "table": settings_table(agent_type, values)}
    suggestion = responses_suggestion(agent_type, values)
    if suggestion:
        report["suggestion"] = suggestion
    return report


__all__ = [
    "MODEL_SETTING_ROWS",
    "effective_values",
    "model_settings_report",
    "responses_suggestion",
    "settings_table",
]
