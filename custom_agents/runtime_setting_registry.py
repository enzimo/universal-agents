"""Trusted setting metadata shared by policy validation and mutation services.

This registry cannot import runtime services: authorization must validate a
setting without constructing the service whose operation it authorizes.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Iterable, Protocol

from custom_messages.contracts.runtime_v1 import (
    RESTART_SCOPE_ALL,
    RESTART_SCOPE_GATEWAY,
    RESTART_SCOPE_MANAGER,
    RESTART_SCOPE_NONE,
)
from runtime_settings.schedule_workers import SCHEDULE_WORKER_ELIGIBILITY_PATH

# Settings under these two prefixes that are stored in LLM_OVERRIDES.yaml need
# no restart: every agent reads that file again at the start of each turn
# (model_failover_runtime.refresh_runtime_llm_selection) and rebuilds its model
# when its profile changed. A change explicitly targeting the applying
# agent's type switches it at once. Workspace-default changes and changes
# for other types reach affected agents at their next turn. A manager restart would not
# help them anyway, because it restarts only TeamLead's process.
CURRENT_AGENT_LLM_PATH_PREFIX = "current_agent.llm."
WORKSPACE_DEFAULT_LLM_PATH_PREFIX = "workspace_default.llm."
INSTANCE_LLM_PATH_PREFIX = "llm."
# How many tools one request to the model the agent uses may carry
# (custom_agents.tool_loading). A fact about the model, stored for that exact
# provider and model under model_capabilities in LLM_OVERRIDES.yaml.
MODEL_TOOL_LIMIT_SETTING_PATH = "model.max_tools_per_request"
MAX_SOUL_BYTES = 128 * 1024


class SettingDecisionClass(StrEnum):
    LEAD_MANAGED = "lead_managed"
    USER_AUTHORIZED = "user_authorized"
    OPERATOR_ONLY = "operator_only"


class SettingTargetScope(StrEnum):
    SELF = "self"
    AGENT_TYPE = "agent_type"
    TEAM = "team"


class SettingPersistenceTarget(StrEnum):
    INSTANCE_CONFIG = "instance_config"
    WORKSPACE_LLM_OVERRIDES = "workspace_llm_overrides"
    TEAM_LEAD_PROFILE = "team_lead_profile"
    GENERATED_AGENT_DEFINITION = "generated_agent_definition"
    TEAM_COMPOSITION = "team_composition"
    # model_capabilities.<provider>.<model id> in LLM_OVERRIDES.yaml, for the model the agent uses.
    MODEL_CAPABILITY = "model_capability"


@dataclass(frozen=True)
class RuntimeSettingDefinition:
    path: str
    value_type: str
    persistence_target: SettingPersistenceTarget
    default_target_scope: SettingTargetScope
    allowed_target_scopes: tuple[SettingTargetScope, ...]
    decision_class: SettingDecisionClass = SettingDecisionClass.LEAD_MANAGED
    restart_scope: str = RESTART_SCOPE_NONE
    agreement_required: bool = True
    summary: str = ""
    minimum: float | int | None = None
    maximum: float | int | None = None
    choices: tuple[str, ...] = ()
    max_utf8_bytes: int | None = None

    def public_metadata(self) -> dict[str, Any]:
        metadata: dict[str, Any] = {
            "type": self.value_type,
            "persistence_target": self.persistence_target.value,
            "default_target_scope": self.default_target_scope.value,
            "allowed_target_scopes": [
                scope.value for scope in self.allowed_target_scopes
            ],
            "decision_class": self.decision_class.value,
            "requires_agreement": self.agreement_required,
            "requires_authorization": self.decision_class
            == SettingDecisionClass.USER_AUTHORIZED,
            "requires_authenticated_authorization_when_enabled": (
                self.decision_class == SettingDecisionClass.USER_AUTHORIZED
            ),
            "available_when_authorization_disabled": True,
            "restart_scope": self.restart_scope,
            "summary": self.summary,
            "minimum": self.minimum,
            "maximum": self.maximum,
            "choices": list(self.choices),
            "max_utf8_bytes": self.max_utf8_bytes,
        }
        return metadata


SELF_OR_AGENT_TYPE = (SettingTargetScope.SELF, SettingTargetScope.AGENT_TYPE)
TEAM_ONLY = (SettingTargetScope.TEAM,)
AGENT_TYPE_ONLY = (SettingTargetScope.AGENT_TYPE,)
SELF_ONLY = (SettingTargetScope.SELF,)


def _setting(
    path: str,
    value_type: str,
    persistence_target: SettingPersistenceTarget,
    default_target_scope: SettingTargetScope,
    allowed_target_scopes: tuple[SettingTargetScope, ...],
    *,
    decision_class: SettingDecisionClass = SettingDecisionClass.LEAD_MANAGED,
    restart_scope: str = RESTART_SCOPE_NONE,
    agreement_required: bool = True,
    summary: str,
    minimum: float | int | None = None,
    maximum: float | int | None = None,
    choices: tuple[str, ...] = (),
    max_utf8_bytes: int | None = None,
) -> RuntimeSettingDefinition:
    return RuntimeSettingDefinition(
        path=path,
        value_type=value_type,
        persistence_target=persistence_target,
        default_target_scope=default_target_scope,
        allowed_target_scopes=allowed_target_scopes,
        decision_class=decision_class,
        restart_scope=restart_scope,
        agreement_required=agreement_required,
        summary=summary,
        minimum=minimum,
        maximum=maximum,
        choices=choices,
        max_utf8_bytes=max_utf8_bytes,
    )


_DEFINITIONS = [
    _setting(
        "current_agent.llm.provider",
        "str",
        SettingPersistenceTarget.WORKSPACE_LLM_OVERRIDES,
        SettingTargetScope.SELF,
        SELF_OR_AGENT_TYPE,
        agreement_required=False,
        summary="LLM provider override for this or one named agent type; applies at once when it targets the agent making the change; other running agents of the target type apply it at their next turn.",
    ),
    _setting(
        "current_agent.llm.model_id",
        "str",
        SettingPersistenceTarget.WORKSPACE_LLM_OVERRIDES,
        SettingTargetScope.SELF,
        SELF_OR_AGENT_TYPE,
        agreement_required=False,
        summary="LLM model override for this or one named agent type; applies at once when it targets the agent making the change; other running agents of the target type apply it at their next turn.",
    ),
    _setting(
        "current_agent.llm.supports_image_input",
        "optional_bool",
        SettingPersistenceTarget.WORKSPACE_LLM_OVERRIDES,
        SettingTargetScope.SELF,
        SELF_OR_AGENT_TYPE,
        agreement_required=False,
        summary="Reviewed image-input capability metadata for this or one named agent type; applies at once when it targets the agent making the change; other running agents of the target type apply it at their next turn.",
    ),
    _setting(
        "current_agent.llm.supports_audio_input",
        "optional_bool",
        SettingPersistenceTarget.WORKSPACE_LLM_OVERRIDES,
        SettingTargetScope.SELF,
        SELF_OR_AGENT_TYPE,
        agreement_required=False,
        summary="Reviewed audio-input capability metadata for this or one named agent type; applies at once when it targets the agent making the change; other running agents of the target type apply it at their next turn.",
    ),
    _setting(
        "current_agent.llm.supports_file_input",
        "optional_bool",
        SettingPersistenceTarget.WORKSPACE_LLM_OVERRIDES,
        SettingTargetScope.SELF,
        SELF_OR_AGENT_TYPE,
        agreement_required=False,
        summary="Reviewed native file-input capability metadata for this or one named agent type; applies at once when it targets the agent making the change; other running agents of the target type apply it at their next turn.",
    ),
    _setting(
        "current_agent.llm.context_window_tokens",
        "optional_int",
        SettingPersistenceTarget.WORKSPACE_LLM_OVERRIDES,
        SettingTargetScope.SELF,
        SELF_OR_AGENT_TYPE,
        agreement_required=False,
        minimum=1_024,
        maximum=2_000_000,
        summary="Context-window override for this or one named agent type; applies at once when it targets the agent making the change; other running agents of the target type apply it at their next turn.",
    ),
    _setting(
        "current_agent.llm.reasoning_effort",
        "optional_str",
        SettingPersistenceTarget.WORKSPACE_LLM_OVERRIDES,
        SettingTargetScope.SELF,
        SELF_OR_AGENT_TYPE,
        max_utf8_bytes=64,
        summary="Reasoning-effort hint for this or one named agent type; model-affecting changes are preflighted; applies at once when it targets the agent making the change; other running agents of the target type apply it at their next turn.",
    ),
    _setting(
        "current_agent.llm.reasoning_summary",
        "optional_str",
        SettingPersistenceTarget.WORKSPACE_LLM_OVERRIDES,
        SettingTargetScope.SELF,
        SELF_OR_AGENT_TYPE,
        choices=("auto", "concise", "detailed"),
        max_utf8_bytes=16,
        summary="Provider-generated OpenAI Responses reasoning summary for this or one named agent type; null disables summaries.",
    ),
    _setting(
        "current_agent.llm.litellm_api_mode",
        "str",
        SettingPersistenceTarget.WORKSPACE_LLM_OVERRIDES,
        SettingTargetScope.SELF,
        SELF_OR_AGENT_TYPE,
        choices=("chat_completions", "responses"),
        max_utf8_bytes=32,
        summary="LiteLLM request API for this or one named agent type; Responses mode is preflighted and requires a Responses-capable endpoint.",
    ),
    _setting(
        "workspace_default.llm.provider",
        "str",
        SettingPersistenceTarget.WORKSPACE_LLM_OVERRIDES,
        SettingTargetScope.TEAM,
        TEAM_ONLY,
        max_utf8_bytes=128,
        summary="Workspace-wide default LLM provider in LLM_OVERRIDES.yaml; overrides the instance-config team baseline for agent types without a more specific workspace override.",
    ),
    _setting(
        "workspace_default.llm.model_id",
        "str",
        SettingPersistenceTarget.WORKSPACE_LLM_OVERRIDES,
        SettingTargetScope.TEAM,
        TEAM_ONLY,
        max_utf8_bytes=512,
        summary="Workspace-wide default model id in LLM_OVERRIDES.yaml; overrides the instance-config team baseline for agent types without a more specific workspace override.",
    ),
    _setting(
        "workspace_default.llm.supports_image_input",
        "optional_bool",
        SettingPersistenceTarget.WORKSPACE_LLM_OVERRIDES,
        SettingTargetScope.TEAM,
        TEAM_ONLY,
        summary="Reviewed workspace-wide image-input capability metadata in LLM_OVERRIDES.yaml.",
    ),
    _setting(
        "workspace_default.llm.supports_audio_input",
        "optional_bool",
        SettingPersistenceTarget.WORKSPACE_LLM_OVERRIDES,
        SettingTargetScope.TEAM,
        TEAM_ONLY,
        summary="Reviewed workspace-wide audio-input capability metadata in LLM_OVERRIDES.yaml.",
    ),
    _setting(
        "workspace_default.llm.supports_file_input",
        "optional_bool",
        SettingPersistenceTarget.WORKSPACE_LLM_OVERRIDES,
        SettingTargetScope.TEAM,
        TEAM_ONLY,
        summary="Reviewed workspace-wide native file-input capability metadata in LLM_OVERRIDES.yaml.",
    ),
    _setting(
        "workspace_default.llm.context_window_tokens",
        "optional_int",
        SettingPersistenceTarget.WORKSPACE_LLM_OVERRIDES,
        SettingTargetScope.TEAM,
        TEAM_ONLY,
        minimum=1_024,
        maximum=2_000_000,
        summary="Workspace-wide context-window override in LLM_OVERRIDES.yaml.",
    ),
    _setting(
        "workspace_default.llm.reasoning_effort",
        "optional_str",
        SettingPersistenceTarget.WORKSPACE_LLM_OVERRIDES,
        SettingTargetScope.TEAM,
        TEAM_ONLY,
        max_utf8_bytes=64,
        summary="Workspace-wide reasoning-effort hint in LLM_OVERRIDES.yaml; model-affecting changes are tested before persistence.",
    ),
    _setting(
        "workspace_default.llm.reasoning_summary",
        "optional_str",
        SettingPersistenceTarget.WORKSPACE_LLM_OVERRIDES,
        SettingTargetScope.TEAM,
        TEAM_ONLY,
        choices=("auto", "concise", "detailed"),
        max_utf8_bytes=16,
        summary="Workspace-wide provider-generated OpenAI Responses reasoning summary; null disables summaries.",
    ),
    _setting(
        "workspace_default.llm.litellm_api_mode",
        "str",
        SettingPersistenceTarget.WORKSPACE_LLM_OVERRIDES,
        SettingTargetScope.TEAM,
        TEAM_ONLY,
        choices=("chat_completions", "responses"),
        max_utf8_bytes=32,
        summary="Workspace-wide LiteLLM request API; Responses mode requires a Responses-capable endpoint.",
    ),
    _setting(
        "current_agent.llm.temperature",
        "optional_float",
        SettingPersistenceTarget.INSTANCE_CONFIG,
        SettingTargetScope.SELF,
        SELF_OR_AGENT_TYPE,
        restart_scope=RESTART_SCOPE_MANAGER,
        minimum=0.0,
        maximum=2.0,
        summary="Sampling-temperature override for this or one named agent type.",
    ),
    _setting(
        "current_agent.llm.max_output_tokens",
        "optional_int",
        SettingPersistenceTarget.INSTANCE_CONFIG,
        SettingTargetScope.SELF,
        SELF_OR_AGENT_TYPE,
        restart_scope=RESTART_SCOPE_MANAGER,
        minimum=1,
        maximum=1_000_000,
        summary="Maximum output-token override for this or one named agent type.",
    ),
    _setting(
        "current_agent.llm.stream",
        "optional_bool",
        SettingPersistenceTarget.INSTANCE_CONFIG,
        SettingTargetScope.SELF,
        SELF_OR_AGENT_TYPE,
        restart_scope=RESTART_SCOPE_MANAGER,
        summary="Streaming override for this or one named agent type.",
    ),
    _setting(
        "current_agent.llm.reasoning_trace_retention_messages",
        "optional_int",
        SettingPersistenceTarget.INSTANCE_CONFIG,
        SettingTargetScope.SELF,
        SELF_OR_AGENT_TYPE,
        restart_scope=RESTART_SCOPE_MANAGER,
        minimum=1,
        maximum=10_000,
        summary="Provider reasoning-trace retention window for this or one named agent type.",
    ),
    _setting(
        "llm.provider",
        "optional_str",
        SettingPersistenceTarget.INSTANCE_CONFIG,
        SettingTargetScope.TEAM,
        TEAM_ONLY,
        restart_scope=RESTART_SCOPE_MANAGER,
        max_utf8_bytes=128,
        summary="Default LLM provider for newly started team agents.",
    ),
    _setting(
        "llm.litellm_api_base",
        "endpoint_url",
        SettingPersistenceTarget.INSTANCE_CONFIG,
        SettingTargetScope.TEAM,
        TEAM_ONLY,
        # Not user_authorized: that would let the person who asked approve
        # sending the whole team's model requests to a new host. Administrators
        # approve it in the Inbox (authorization on); agreement in chat otherwise.
        # No restart: every agent applies it at its next turn (custom_agents.endpoint_refresh).
        restart_scope=RESTART_SCOPE_NONE,
        max_utf8_bytes=512,
        summary=(
            "The team's OpenAI-compatible model endpoint (provider litellm), such as OpenRouter, a LiteLLM proxy "
            "or Bifrost. Its key is the one saved for the new address with /auth model; staging is refused when "
            "there is none or when LITELLM_API_BASE in the environment would override the change. An "
            "administrator approves it, the endpoint is tested before it is saved, and every agent switches at "
            "its next turn, without a restart."
        ),
    ),
    _setting(
        "llm.model_id",
        "optional_str",
        SettingPersistenceTarget.INSTANCE_CONFIG,
        SettingTargetScope.TEAM,
        TEAM_ONLY,
        restart_scope=RESTART_SCOPE_MANAGER,
        max_utf8_bytes=512,
        summary="Default LLM model id for newly started team agents.",
    ),
    _setting(
        "llm.temperature",
        "optional_float",
        SettingPersistenceTarget.INSTANCE_CONFIG,
        SettingTargetScope.TEAM,
        TEAM_ONLY,
        restart_scope=RESTART_SCOPE_MANAGER,
        minimum=0.0,
        maximum=2.0,
        summary="Default team sampling temperature.",
    ),
    _setting(
        "llm.max_output_tokens",
        "optional_int",
        SettingPersistenceTarget.INSTANCE_CONFIG,
        SettingTargetScope.TEAM,
        TEAM_ONLY,
        restart_scope=RESTART_SCOPE_MANAGER,
        minimum=1,
        maximum=1_000_000,
        summary="Default team output-token cap.",
    ),
    _setting(
        "llm.context_window_tokens",
        "optional_int",
        SettingPersistenceTarget.INSTANCE_CONFIG,
        SettingTargetScope.TEAM,
        TEAM_ONLY,
        restart_scope=RESTART_SCOPE_MANAGER,
        minimum=1_024,
        maximum=2_000_000,
        summary="Default team context-window size.",
    ),
    _setting(
        "llm.stream",
        "optional_bool",
        SettingPersistenceTarget.INSTANCE_CONFIG,
        SettingTargetScope.TEAM,
        TEAM_ONLY,
        restart_scope=RESTART_SCOPE_MANAGER,
        summary="Default team LiteLLM streaming setting.",
    ),
    _setting(
        "llm.supports_image_input",
        "optional_bool",
        SettingPersistenceTarget.INSTANCE_CONFIG,
        SettingTargetScope.TEAM,
        TEAM_ONLY,
        restart_scope=RESTART_SCOPE_MANAGER,
        summary="Default team image-input capability metadata.",
    ),
    _setting(
        "llm.supports_audio_input",
        "optional_bool",
        SettingPersistenceTarget.INSTANCE_CONFIG,
        SettingTargetScope.TEAM,
        TEAM_ONLY,
        restart_scope=RESTART_SCOPE_MANAGER,
        summary="Default team audio-input capability metadata.",
    ),
    _setting(
        "llm.supports_file_input",
        "optional_bool",
        SettingPersistenceTarget.INSTANCE_CONFIG,
        SettingTargetScope.TEAM,
        TEAM_ONLY,
        restart_scope=RESTART_SCOPE_MANAGER,
        summary="Default team native file-input capability metadata.",
    ),
    _setting(
        "llm.reasoning_effort",
        "optional_str",
        SettingPersistenceTarget.INSTANCE_CONFIG,
        SettingTargetScope.TEAM,
        TEAM_ONLY,
        restart_scope=RESTART_SCOPE_MANAGER,
        max_utf8_bytes=64,
        summary="Default team reasoning-effort hint.",
    ),
    _setting(
        "llm.reasoning_summary",
        "optional_str",
        SettingPersistenceTarget.INSTANCE_CONFIG,
        SettingTargetScope.TEAM,
        TEAM_ONLY,
        restart_scope=RESTART_SCOPE_MANAGER,
        choices=("auto", "concise", "detailed"),
        max_utf8_bytes=16,
        summary="Default team provider-generated OpenAI Responses reasoning summary; null disables summaries.",
    ),
    _setting(
        "llm.litellm_api_mode",
        "str",
        SettingPersistenceTarget.INSTANCE_CONFIG,
        SettingTargetScope.TEAM,
        TEAM_ONLY,
        restart_scope=RESTART_SCOPE_MANAGER,
        choices=("chat_completions", "responses"),
        max_utf8_bytes=32,
        summary="Default team LiteLLM request API; Responses mode requires a Responses-capable endpoint.",
    ),
    _setting(
        "llm.reasoning_trace_retention_messages",
        "optional_int",
        SettingPersistenceTarget.INSTANCE_CONFIG,
        SettingTargetScope.TEAM,
        TEAM_ONLY,
        restart_scope=RESTART_SCOPE_MANAGER,
        minimum=1,
        maximum=10_000,
        summary="Default team provider reasoning-trace retention window.",
    ),
    _setting(
        "heartbeat.enabled",
        "bool",
        SettingPersistenceTarget.INSTANCE_CONFIG,
        SettingTargetScope.TEAM,
        TEAM_ONLY,
        restart_scope=RESTART_SCOPE_ALL,
        summary="Enable or disable the gateway-installed heartbeat launcher.",
    ),
    _setting(
        "heartbeat.interval_seconds",
        "int",
        SettingPersistenceTarget.INSTANCE_CONFIG,
        SettingTargetScope.TEAM,
        TEAM_ONLY,
        restart_scope=RESTART_SCOPE_ALL,
        minimum=1,
        maximum=86_400,
        summary="Heartbeat launcher interval in seconds.",
    ),
    _setting(
        "heartbeat.active_hours_enabled",
        "bool",
        SettingPersistenceTarget.INSTANCE_CONFIG,
        SettingTargetScope.TEAM,
        TEAM_ONLY,
        restart_scope=RESTART_SCOPE_ALL,
        summary="Enable heartbeat active-hour filtering.",
    ),
    _setting(
        "heartbeat.active_start_hour_local",
        "int",
        SettingPersistenceTarget.INSTANCE_CONFIG,
        SettingTargetScope.TEAM,
        TEAM_ONLY,
        restart_scope=RESTART_SCOPE_ALL,
        minimum=0,
        maximum=23,
        summary="Local hour when the heartbeat active window starts.",
    ),
    _setting(
        "heartbeat.active_end_hour_local",
        "int",
        SettingPersistenceTarget.INSTANCE_CONFIG,
        SettingTargetScope.TEAM,
        TEAM_ONLY,
        restart_scope=RESTART_SCOPE_ALL,
        minimum=0,
        maximum=23,
        summary="Local hour when the heartbeat active window ends.",
    ),
    _setting(
        "delivery.default_target.channel_name",
        "str",
        SettingPersistenceTarget.INSTANCE_CONFIG,
        SettingTargetScope.TEAM,
        TEAM_ONLY,
        decision_class=SettingDecisionClass.USER_AUTHORIZED,
        restart_scope=RESTART_SCOPE_GATEWAY,
        choices=("cli", "discord", "telegram", "whatsapp", "signal", "photon"),
        summary="Change the proactive delivery channel. This changes the recipient boundary and needs authenticated user authorization when authorization is enabled.",
    ),
    _setting(
        "delivery.default_target.chat_id",
        "str",
        SettingPersistenceTarget.INSTANCE_CONFIG,
        SettingTargetScope.TEAM,
        TEAM_ONLY,
        decision_class=SettingDecisionClass.USER_AUTHORIZED,
        restart_scope=RESTART_SCOPE_GATEWAY,
        max_utf8_bytes=512,
        summary="Change the proactive delivery recipient. This needs authenticated user authorization when authorization is enabled.",
    ),
    _setting(
        "delivery.default_target.thread_id",
        "str",
        SettingPersistenceTarget.INSTANCE_CONFIG,
        SettingTargetScope.TEAM,
        TEAM_ONLY,
        decision_class=SettingDecisionClass.USER_AUTHORIZED,
        restart_scope=RESTART_SCOPE_GATEWAY,
        max_utf8_bytes=512,
        summary="Change the proactive delivery thread/topic. This needs authenticated user authorization when authorization is enabled.",
    ),
    _setting(
        "delivery.notification_routing.mode",
        "str",
        SettingPersistenceTarget.INSTANCE_CONFIG,
        SettingTargetScope.TEAM,
        TEAM_ONLY,
        restart_scope=RESTART_SCOPE_GATEWAY,
        choices=("source_thread_or_default", "default_target"),
        summary="Default notify_user routing mode.",
    ),
    _setting(
        "delivery.quiet_hours.enabled",
        "bool",
        SettingPersistenceTarget.INSTANCE_CONFIG,
        SettingTargetScope.TEAM,
        TEAM_ONLY,
        restart_scope=RESTART_SCOPE_GATEWAY,
        summary="Enable or disable quiet-hours deferral.",
    ),
    _setting(
        "delivery.quiet_hours.start_hour_local",
        "int",
        SettingPersistenceTarget.INSTANCE_CONFIG,
        SettingTargetScope.TEAM,
        TEAM_ONLY,
        restart_scope=RESTART_SCOPE_GATEWAY,
        minimum=0,
        maximum=23,
        summary="Local quiet-hours start hour.",
    ),
    _setting(
        "delivery.quiet_hours.end_hour_local",
        "int",
        SettingPersistenceTarget.INSTANCE_CONFIG,
        SettingTargetScope.TEAM,
        TEAM_ONLY,
        restart_scope=RESTART_SCOPE_GATEWAY,
        minimum=0,
        maximum=23,
        summary="Local quiet-hours end hour.",
    ),
    _setting(
        "delivery.quiet_hours.allow_urgent_delivery",
        "bool",
        SettingPersistenceTarget.INSTANCE_CONFIG,
        SettingTargetScope.TEAM,
        TEAM_ONLY,
        restart_scope=RESTART_SCOPE_GATEWAY,
        summary="Allow urgent messages to bypass quiet hours.",
    ),
    _setting(
        "delivery.digest_delivery_hour_local",
        "int",
        SettingPersistenceTarget.INSTANCE_CONFIG,
        SettingTargetScope.TEAM,
        TEAM_ONLY,
        restart_scope=RESTART_SCOPE_GATEWAY,
        minimum=0,
        maximum=23,
        summary="Local deferred-message digest hour.",
    ),
    _setting(
        "delivery.summarize_deferred",
        "bool",
        SettingPersistenceTarget.INSTANCE_CONFIG,
        SettingTargetScope.TEAM,
        TEAM_ONLY,
        restart_scope=RESTART_SCOPE_GATEWAY,
        summary="Summarize deferred messages before digest delivery.",
    ),
    _setting(
        "delivery.urgent_keywords",
        "list_str",
        SettingPersistenceTarget.INSTANCE_CONFIG,
        SettingTargetScope.TEAM,
        TEAM_ONLY,
        restart_scope=RESTART_SCOPE_GATEWAY,
        summary="Keywords that mark proactive delivery as urgent.",
    ),
    _setting(
        "agent.memory.notifications.delivery_target",
        "str",
        SettingPersistenceTarget.INSTANCE_CONFIG,
        SettingTargetScope.TEAM,
        TEAM_ONLY,
        restart_scope=RESTART_SCOPE_MANAGER,
        choices=("default", "current_thread"),
        summary="Delivery target for memory/topic notices.",
    ),
    _setting(
        "voice.enabled",
        "bool",
        SettingPersistenceTarget.INSTANCE_CONFIG,
        SettingTargetScope.TEAM,
        TEAM_ONLY,
        restart_scope=RESTART_SCOPE_GATEWAY,
        summary="Enable or disable gateway voice features.",
    ),
    _setting(
        "voice.input_enabled",
        "bool",
        SettingPersistenceTarget.INSTANCE_CONFIG,
        SettingTargetScope.TEAM,
        TEAM_ONLY,
        restart_scope=RESTART_SCOPE_GATEWAY,
        summary="Enable inbound speech-to-text.",
    ),
    _setting(
        "voice.output_enabled",
        "bool",
        SettingPersistenceTarget.INSTANCE_CONFIG,
        SettingTargetScope.TEAM,
        TEAM_ONLY,
        restart_scope=RESTART_SCOPE_GATEWAY,
        summary="Enable text-to-speech for voice-originated replies.",
    ),
    _setting(
        "voice.tts_voice",
        "str",
        SettingPersistenceTarget.INSTANCE_CONFIG,
        SettingTargetScope.TEAM,
        TEAM_ONLY,
        restart_scope=RESTART_SCOPE_GATEWAY,
        max_utf8_bytes=256,
        summary="Voice name used for text-to-speech.",
    ),
    _setting(
        "agent.max_tool_calls_per_task",
        "optional_int",
        SettingPersistenceTarget.INSTANCE_CONFIG,
        SettingTargetScope.TEAM,
        TEAM_ONLY,
        restart_scope=RESTART_SCOPE_MANAGER,
        minimum=1,
        maximum=1_000,
        summary="Cumulative tool calls across one local reasoning pass, not per model turn.",
    ),
    _setting(
        "agent.max_tool_result_tokens_per_call",
        "optional_int",
        SettingPersistenceTarget.INSTANCE_CONFIG,
        SettingTargetScope.TEAM,
        TEAM_ONLY,
        restart_scope=RESTART_SCOPE_MANAGER,
        minimum=1,
        maximum=1_000_000,
        summary="Maximum rendered tokens per tool result.",
    ),
    _setting(
        "agent.max_queued_top_level_tasks",
        "int",
        SettingPersistenceTarget.INSTANCE_CONFIG,
        SettingTargetScope.TEAM,
        TEAM_ONLY,
        restart_scope=RESTART_SCOPE_MANAGER,
        minimum=1,
        maximum=10_000,
        summary="Maximum queued top-level tasks.",
    ),
    _setting(
        "agent.max_concurrent_top_level_tasks",
        "int",
        SettingPersistenceTarget.INSTANCE_CONFIG,
        SettingTargetScope.TEAM,
        TEAM_ONLY,
        restart_scope=RESTART_SCOPE_MANAGER,
        minimum=1,
        maximum=128,
        summary="Maximum concurrent top-level tasks.",
    ),
    _setting(
        "agent.task_queue_warning_threshold",
        "int",
        SettingPersistenceTarget.INSTANCE_CONFIG,
        SettingTargetScope.TEAM,
        TEAM_ONLY,
        restart_scope=RESTART_SCOPE_MANAGER,
        minimum=1,
        maximum=10_000,
        summary="Queued-task pressure warning threshold.",
    ),
    _setting(
        "agent.subtask_timeout_seconds",
        "optional_float",
        SettingPersistenceTarget.INSTANCE_CONFIG,
        SettingTargetScope.TEAM,
        TEAM_ONLY,
        restart_scope=RESTART_SCOPE_MANAGER,
        minimum=1.0,
        maximum=604_800.0,
        summary="Default delegated-subtask timeout.",
    ),
    _setting(
        "agent.debug.context_dump.enabled",
        "bool",
        SettingPersistenceTarget.INSTANCE_CONFIG,
        SettingTargetScope.TEAM,
        TEAM_ONLY,
        decision_class=SettingDecisionClass.USER_AUTHORIZED,
        summary="Enable context debug dumps, which may contain sensitive runtime material and therefore need authenticated user authorization when authorization is enabled.",
    ),
    _setting(
        "agent.debug.context_dump.mode",
        "context_dump_mode",
        SettingPersistenceTarget.INSTANCE_CONFIG,
        SettingTargetScope.TEAM,
        TEAM_ONLY,
        decision_class=SettingDecisionClass.USER_AUTHORIZED,
        choices=("always", "on_error", "on_budget_warning"),
        summary="Change the context-dump trigger mode; context dumps need authenticated user authorization when authorization is enabled.",
    ),
    _setting(
        "team_lead.soul",
        "text",
        SettingPersistenceTarget.TEAM_LEAD_PROFILE,
        SettingTargetScope.SELF,
        SELF_ONLY,
        restart_scope=RESTART_SCOPE_NONE,
        max_utf8_bytes=MAX_SOUL_BYTES,
        summary="Replace TeamArchitect's behavioral SOUL.md profile. This cannot grant authority.",
    ),
    _setting(
        "specialist.display_name",
        "str",
        SettingPersistenceTarget.GENERATED_AGENT_DEFINITION,
        SettingTargetScope.AGENT_TYPE,
        AGENT_TYPE_ONLY,
        restart_scope=RESTART_SCOPE_MANAGER,
        max_utf8_bytes=512,
        summary="Change a generated specialist's display name.",
    ),
    _setting(
        "specialist.role",
        "str",
        SettingPersistenceTarget.GENERATED_AGENT_DEFINITION,
        SettingTargetScope.AGENT_TYPE,
        AGENT_TYPE_ONLY,
        restart_scope=RESTART_SCOPE_MANAGER,
        max_utf8_bytes=4_096,
        summary="Change a generated specialist's behavioral role description.",
    ),
    _setting(
        "specialist.description",
        "str",
        SettingPersistenceTarget.GENERATED_AGENT_DEFINITION,
        SettingTargetScope.AGENT_TYPE,
        AGENT_TYPE_ONLY,
        restart_scope=RESTART_SCOPE_MANAGER,
        max_utf8_bytes=16_384,
        summary="Change a generated specialist's catalog description.",
    ),
    _setting(
        "specialist.prompt",
        "text",
        SettingPersistenceTarget.GENERATED_AGENT_DEFINITION,
        SettingTargetScope.AGENT_TYPE,
        AGENT_TYPE_ONLY,
        restart_scope=RESTART_SCOPE_MANAGER,
        max_utf8_bytes=MAX_SOUL_BYTES,
        summary="Change a generated specialist's behavioral prompt; tool and capability fields are excluded.",
    ),
    _setting(
        "team.startup_agents.count",
        "int",
        SettingPersistenceTarget.TEAM_COMPOSITION,
        SettingTargetScope.AGENT_TYPE,
        AGENT_TYPE_ONLY,
        restart_scope=RESTART_SCOPE_ALL,
        minimum=1,
        maximum=32,
        summary="Set the next-launch startup count for one named agent type.",
    ),
    _setting(
        "team.startup_agents.enabled",
        "bool",
        SettingPersistenceTarget.TEAM_COMPOSITION,
        SettingTargetScope.AGENT_TYPE,
        AGENT_TYPE_ONLY,
        restart_scope=RESTART_SCOPE_ALL,
        summary="Enable or disable one named agent type in the next-launch startup roster.",
    ),
    _setting(
        "team.startup_agents.mode",
        "str",
        SettingPersistenceTarget.TEAM_COMPOSITION,
        SettingTargetScope.TEAM,
        TEAM_ONLY,
        restart_scope=RESTART_SCOPE_ALL,
        choices=("merge", "replace"),
        summary="Choose whether startup roster overrides merge with or replace base startup agents.",
    ),
    _setting(
        "team.ephemeral_agents.enabled",
        "bool",
        SettingPersistenceTarget.TEAM_COMPOSITION,
        SettingTargetScope.AGENT_TYPE,
        AGENT_TYPE_ONLY,
        summary="Enable or disable ephemeral idle cleanup for one named agent type.",
    ),
    _setting(
        "team.ephemeral_agents.idle_ttl_seconds",
        "optional_float",
        SettingPersistenceTarget.TEAM_COMPOSITION,
        SettingTargetScope.AGENT_TYPE,
        AGENT_TYPE_ONLY,
        minimum=1.0,
        maximum=86_400.0,
        summary="Set the idle TTL for future dynamic instances of one named agent type.",
    ),
    _setting(
        "team.ephemeral_agents.default_idle_ttl_seconds",
        "optional_float",
        SettingPersistenceTarget.TEAM_COMPOSITION,
        SettingTargetScope.TEAM,
        TEAM_ONLY,
        minimum=1.0,
        maximum=86_400.0,
        summary="Set the default idle TTL for future dynamic agents.",
    ),
]

# Eligibility admits registered specialists to consideration, not to execution.
# Explicit denies, launch authority, limits and the kill switch remain independent.
_DEFINITIONS.append(_setting(
    SCHEDULE_WORKER_ELIGIBILITY_PATH,
    "list_str",
    SettingPersistenceTarget.INSTANCE_CONFIG,
    SettingTargetScope.TEAM,
    TEAM_ONLY,
    agreement_required=False,
    summary="Set automatic-start candidates, preserving existing entries when adding a registered custom specialist. Operator denies and current launch authority still apply; configure each schedule separately.",
))

for _rule_name, _value_type, _minimum, _maximum in (
    ("claim_wait_seconds", "float", 0, 86400),
    ("startup_timeout_seconds", "float", 1, 3600),
    ("admission_timeout_seconds", "float", 1, 3600),
    ("max_launch_attempts_per_occurrence", "int", 1, 10),
    ("retry_backoff_seconds", "float", 1, 300),
    ("launch_when_busy", "bool", None, None),
    ("idle_ttl_seconds", "optional_float", 1, 86400),
    ("interrupted_run_skip_window_seconds", "float", 0, 86400),
):
    for _prefix, _scope, _scopes in (
        ("agent.schedule_workers.defaults", SettingTargetScope.TEAM, TEAM_ONLY),
        ("agent.schedule_worker_rules", SettingTargetScope.AGENT_TYPE, AGENT_TYPE_ONLY),
    ):
        _DEFINITIONS.append(_setting(
            f"{_prefix}.{_rule_name}", _value_type, SettingPersistenceTarget.INSTANCE_CONFIG,
            _scope, _scopes, minimum=_minimum, maximum=_maximum,
            agreement_required=False,
            summary=f"Set {_rule_name} for scheduled workers within current operator limits.",
        ))

# Raising the tool cap is how TeamLead asks for more tools at once, for its
# own model or an agent type's (custom_agents.model_tool_limit). The cap
# belongs to the model, so it changes every agent on that model in everyone's
# work: like the team's model endpoint it is not USER_AUTHORIZED, so with
# authorization on an administrator approves it in the Inbox (with it off, the
# person agrees in chat). It only raises, and applies only after one request
# with that many tools succeeded, so it never passes what the model takes.
_DEFINITIONS.append(_setting(
    MODEL_TOOL_LIMIT_SETTING_PATH, "int", SettingPersistenceTarget.MODEL_CAPABILITY,
    SettingTargetScope.SELF, SELF_OR_AGENT_TYPE,
    minimum=1, maximum=4_096,
    summary=(
        "Raise how many tools one request to a model may carry (max_tools_per_request): the model you use now, "
        "or with target_agent_type the model that agent type runs. It applies to every agent on that model; "
        "an administrator approves it, and it applies only after one request with that many tools succeeds."
    ),
))

RUNTIME_SETTING_REGISTRY: dict[str, RuntimeSettingDefinition] = {
    definition.path: definition for definition in _DEFINITIONS
}

# Who a team-default model change reaches, as a person reads it.
TEAM_DEFAULT_AGENTS_WORDS = "agents that use the team default"
NEXT_TURN_TIMING = "Takes effect at each running agent's next turn."


class _PlannedModelChange(Protocol):
    path: str
    persistence_target: SettingPersistenceTarget
    target_agent_type: str | None


def next_turn_targets(changes: Iterable[_PlannedModelChange], actor_agent_type: str) -> list[str]:
    """Who a plan's model settings reach only at their next turn, as a person names them.

    These are settings stored in LLM_OVERRIDES.yaml for another agent type or
    for the team default (see the note at CURRENT_AGENT_LLM_PATH_PREFIX).
    An explicit change for the applying agent's type switches it at once,
    so that target is left out. A default-only change also reaches the
    applying agent at its next turn when it inherits the changed setting.
    """

    targets: dict[str, None] = {}
    for change in changes:
        if change.persistence_target != SettingPersistenceTarget.WORKSPACE_LLM_OVERRIDES:
            continue
        if change.path.startswith(WORKSPACE_DEFAULT_LLM_PATH_PREFIX):
            targets[TEAM_DEFAULT_AGENTS_WORDS] = None
        elif change.target_agent_type and change.target_agent_type != actor_agent_type:
            targets[f"{change.target_agent_type} agents"] = None
    return list(targets)


def next_turn_note(targets: Iterable[str]) -> str:
    """The sentence telling a person when other running agents use a saved model change."""

    return (
        f"Running {' and '.join(targets)} switch at their next turn; no restart is needed. "
        "Until then, their status shows the previous values."
    )

OPERATOR_ONLY_RUNTIME_SETTINGS: dict[str, str] = {
    "agent.schedule_workers.*": "The kill switch, explicit type denies, capacity, rate limits, leases and timing ceilings are operator-owned; eligible_agent_types and the listed defaults are TeamLead-editable.",
    "agent.worker_pools.*": (
        "Worker pool ceilings, start rates and controller timings are operator-owned. "
        "TeamArchitect sets each worker type's pool size inside the ceilings with "
        "set_worker_pool_limit, which stores it in the database, not here."
    ),
    "transport.*": "NATS routing and credentials are operator-owned.",
    "gateway.storage.*": "Gateway storage controls persisted sessions and must be changed by an operator.",
    "state.storage.*": "Manager storage controls shared persistence and must be changed by an operator.",
    "security_layer.*": "Security mode, credential directories, and sandbox policy are operator-owned.",
    "tool_policy.*": "Tool settings such as how long scratch files are kept decide what trusted maintenance deletes from people's folders; only the operator changes them.",
    "authorization.*": "Authorization settings, such as turning it on or off and the team lead's starter profile, are operator-owned.",
    "interactive_apps.plugins.*.settings.enabled": (
        "App process lifecycle and credential wiring are operator-owned."
    ),
    "interactive_apps.plugins.builtin.internal_website.settings.bind_*": (
        "Listener bind settings are deployment-owned."
    ),
    "activity_collector.*": "Collector process settings are deployment-owned.",
    "instance_root": "Instance root is immutable for a running instance.",
    "agent.instance_root": "The instance root decides where control files, databases and agent folders live and is operator-owned.",
    "agent.tool_allowlist": "Tool attachment policy is operator/user-authorized, not lead-managed.",
    "agent.tool_blocklist": "Tool attachment policy is operator/user-authorized, not lead-managed.",
}
