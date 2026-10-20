"""Interactive apps configuration and contracts."""

from __future__ import annotations
import re
from pathlib import Path
from typing import Any, Dict, Mapping, Optional
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from default_values import INTERACTIVE_APP_DEFAULTS
from assistant_gateway.interactive_apps.identifiers import (
    BUILTIN_DISCORD_PLUGIN_ID,
    BUILTIN_INTERACTIVE_APP_PLUGIN_IDS,
    BUILTIN_INTERNAL_WEBSITE_PLUGIN_ID,
    BUILTIN_PHOTON_PLUGIN_ID,
    BUILTIN_SIGNAL_PLUGIN_ID,
    BUILTIN_TELEGRAM_PLUGIN_ID,
    BUILTIN_WHATSAPP_PLUGIN_ID,
    normalize_distribution_name,
    normalize_interactive_app_plugin_id,
)
from runtime_settings.common import (
    DEFAULT_DISCORD_GROUP_POLICY,
    DEFAULT_TELEGRAM_MANAGER_OFFLINE_POLICY,
    TELEGRAM_MANAGER_OFFLINE_POLICY_NOTIFY,
    TELEGRAM_MANAGER_OFFLINE_POLICY_PAUSE_POLLING,
    _normalized_optional_string,
    _normalized_string_list,
)


def default_signal_app_config() -> Dict[str, Any]:
    """Return disabled Signal adapter defaults for config migrations."""
    return {
        "enabled": False,
        "base_url": INTERACTIVE_APP_DEFAULTS.signal_base_url.value,
        "account": "",
        "allowed_sender_ids": [],
        "allowed_group_ids": [],
        "group_policy": INTERACTIVE_APP_DEFAULTS.signal_group_policy.value,
        "mention_markers": [],
        "poll_timeout_seconds": INTERACTIVE_APP_DEFAULTS.signal_poll_timeout_seconds.value,
        "poll_interval_seconds": INTERACTIVE_APP_DEFAULTS.signal_poll_interval_seconds.value,
        "max_messages_per_poll": INTERACTIVE_APP_DEFAULTS.signal_max_messages_per_poll.value,
        "request_timeout_seconds": INTERACTIVE_APP_DEFAULTS.signal_request_timeout_seconds.value,
        "ignore_attachments": INTERACTIVE_APP_DEFAULTS.signal_ignore_attachments.value,
        "ignore_stories": INTERACTIVE_APP_DEFAULTS.signal_ignore_stories.value,
        "ignore_avatars": INTERACTIVE_APP_DEFAULTS.signal_ignore_avatars.value,
        "ignore_stickers": INTERACTIVE_APP_DEFAULTS.signal_ignore_stickers.value,
        "send_read_receipts": INTERACTIVE_APP_DEFAULTS.signal_send_read_receipts.value,
        "drop_pending_messages_on_startup": INTERACTIVE_APP_DEFAULTS.signal_drop_pending_messages_on_startup.value,
        "text_mode": INTERACTIVE_APP_DEFAULTS.signal_text_mode.value,
    }


def default_photon_app_config() -> Dict[str, Any]:
    """Return disabled Photon adapter defaults for config migrations."""
    return {
        "enabled": False,
        "project_id": "",
        "project_id_env_var": INTERACTIVE_APP_DEFAULTS.photon_project_id_env_var.value,
        "project_secret_env_var": INTERACTIVE_APP_DEFAULTS.photon_project_secret_env_var.value,
        "sidecar_token_env_var": INTERACTIVE_APP_DEFAULTS.photon_sidecar_token_env_var.value,
        "sidecar_bind_host": INTERACTIVE_APP_DEFAULTS.photon_sidecar_bind_host.value,
        "sidecar_port": INTERACTIVE_APP_DEFAULTS.photon_sidecar_port.value,
        "sidecar_autostart": INTERACTIVE_APP_DEFAULTS.photon_sidecar_autostart.value,
        "node_bin": INTERACTIVE_APP_DEFAULTS.photon_node_bin.value,
        "allowed_sender_ids": [],
        "allowed_group_ids": [],
        "group_policy": INTERACTIVE_APP_DEFAULTS.photon_group_policy.value,
        "mention_markers": [],
        "request_timeout_seconds": INTERACTIVE_APP_DEFAULTS.photon_request_timeout_seconds.value,
        "max_inline_attachment_bytes": INTERACTIVE_APP_DEFAULTS.photon_max_inline_attachment_bytes.value,
        "markdown": INTERACTIVE_APP_DEFAULTS.photon_markdown.value,
        "telemetry": INTERACTIVE_APP_DEFAULTS.photon_telemetry.value,
    }


class TelegramAppSettings(BaseModel):
    enabled: bool = False
    allowed_user_ids: list[str] = Field(default_factory=list)
    allowed_chat_ids: list[str] = Field(default_factory=list)
    group_policy: str = INTERACTIVE_APP_DEFAULTS.group_policy.value
    manager_offline_policy: str = DEFAULT_TELEGRAM_MANAGER_OFFLINE_POLICY
    reply_to_message: bool = INTERACTIVE_APP_DEFAULTS.reply_to_message.value
    topic_scoped_sessions: bool = INTERACTIVE_APP_DEFAULTS.scoped_sessions.value
    drop_pending_updates: bool = INTERACTIVE_APP_DEFAULTS.drop_pending_updates.value
    rich_messages: bool = True
    proxy: Optional[str] = None
    connection_pool_size: int = (
        INTERACTIVE_APP_DEFAULTS.telegram_connection_pool_size.value
    )
    concurrent_updates: int = INTERACTIVE_APP_DEFAULTS.telegram_concurrent_updates.value
    pool_timeout: float = INTERACTIVE_APP_DEFAULTS.telegram_pool_timeout.value

    @field_validator("group_policy")
    @classmethod
    def _validate_group_policy(cls, value: str) -> str:
        normalized = (value or "").strip().lower()
        if normalized not in {"open", "mention"}:
            raise ValueError("Telegram group_policy must be 'open' or 'mention'.")
        return normalized

    @field_validator("allowed_user_ids", "allowed_chat_ids", mode="before")
    @classmethod
    def _normalize_allowed_ids(cls, value: Any) -> list[str]:
        return _normalized_string_list(value)

    @field_validator("manager_offline_policy")
    @classmethod
    def _validate_manager_offline_policy(cls, value: str) -> str:
        normalized = (value or "").strip().lower()
        if normalized not in {
            TELEGRAM_MANAGER_OFFLINE_POLICY_NOTIFY,
            TELEGRAM_MANAGER_OFFLINE_POLICY_PAUSE_POLLING,
        }:
            raise ValueError(
                "Telegram manager_offline_policy must be 'notify' or 'pause_telegram_polling'."
            )
        return normalized

    @field_validator("concurrent_updates")
    @classmethod
    def _validate_concurrent_updates(cls, value: int) -> int:
        parsed = int(value)
        if parsed < 1:
            raise ValueError("Telegram concurrent_updates must be at least 1.")
        return parsed


class DiscordAppSettings(BaseModel):
    enabled: bool = False
    allowed_user_ids: list[str] = Field(default_factory=list)
    allowed_channel_ids: list[str] = Field(default_factory=list)
    group_policy: str = DEFAULT_DISCORD_GROUP_POLICY
    reply_to_message: bool = INTERACTIVE_APP_DEFAULTS.reply_to_message.value
    thread_scoped_sessions: bool = INTERACTIVE_APP_DEFAULTS.scoped_sessions.value
    command_guild_ids: list[str] = Field(default_factory=list)

    @field_validator("group_policy")
    @classmethod
    def _validate_group_policy(cls, value: str) -> str:
        normalized = (value or "").strip().lower()
        if normalized not in {"open", "mention"}:
            raise ValueError("Discord group_policy must be 'open' or 'mention'.")
        return normalized

    @field_validator(
        "allowed_user_ids", "allowed_channel_ids", "command_guild_ids", mode="before"
    )
    @classmethod
    def _normalize_id_lists(cls, value: Any) -> list[str]:
        return _normalized_string_list(value)


class WhatsAppAppSettings(BaseModel):
    enabled: bool = False
    allowed_sender_ids: list[str] = Field(default_factory=list)
    webhook_host: str = INTERACTIVE_APP_DEFAULTS.whatsapp_webhook_host.value
    webhook_port: int = INTERACTIVE_APP_DEFAULTS.whatsapp_webhook_port.value
    webhook_path: str = INTERACTIVE_APP_DEFAULTS.whatsapp_webhook_path.value
    public_base_url: Optional[str] = None
    validate_twilio_signature: bool = (
        INTERACTIVE_APP_DEFAULTS.validate_twilio_signature.value
    )

    @field_validator("allowed_sender_ids", mode="before")
    @classmethod
    def _normalize_allowed_sender_ids(cls, value: Any) -> list[str]:
        normalized: list[str] = []
        for raw in _normalized_string_list(value):
            sender = str(raw or "").strip()
            if not sender:
                continue
            if sender.lower().startswith("whatsapp:"):
                sender = sender.split(":", 1)[1].strip()
            normalized.append(sender)
        return normalized

    @field_validator("webhook_host", mode="before")
    @classmethod
    def _normalize_webhook_host(cls, value: Any) -> str:
        normalized = str(value or "").strip()
        return normalized or INTERACTIVE_APP_DEFAULTS.whatsapp_webhook_host.value

    @field_validator("webhook_port", mode="before")
    @classmethod
    def _normalize_webhook_port(cls, value: Any) -> int:
        return int(value)

    @field_validator("webhook_port")
    @classmethod
    def _validate_webhook_port(cls, value: int) -> int:
        if value < 1 or value > 65535:
            raise ValueError("WhatsApp webhook_port must be between 1 and 65535.")
        return value

    @field_validator("webhook_path", mode="before")
    @classmethod
    def _normalize_webhook_path(cls, value: Any) -> str:
        normalized = str(value or "").strip()
        if not normalized:
            return INTERACTIVE_APP_DEFAULTS.whatsapp_webhook_path.value
        if not normalized.startswith("/"):
            normalized = f"/{normalized}"
        return normalized

    @field_validator("public_base_url", mode="before")
    @classmethod
    def _normalize_public_base_url(cls, value: Any) -> Optional[str]:
        normalized = _normalized_optional_string(value)
        if not normalized:
            return None
        return normalized.rstrip("/")


class SignalAppSettings(BaseModel):
    enabled: bool = False
    base_url: str = INTERACTIVE_APP_DEFAULTS.signal_base_url.value
    account: str = ""
    allowed_sender_ids: list[str] = Field(default_factory=list)
    allowed_group_ids: list[str] = Field(default_factory=list)
    group_policy: str = INTERACTIVE_APP_DEFAULTS.signal_group_policy.value
    mention_markers: list[str] = Field(default_factory=list)
    poll_timeout_seconds: float = (
        INTERACTIVE_APP_DEFAULTS.signal_poll_timeout_seconds.value
    )
    poll_interval_seconds: float = (
        INTERACTIVE_APP_DEFAULTS.signal_poll_interval_seconds.value
    )
    max_messages_per_poll: int = (
        INTERACTIVE_APP_DEFAULTS.signal_max_messages_per_poll.value
    )
    request_timeout_seconds: float = (
        INTERACTIVE_APP_DEFAULTS.signal_request_timeout_seconds.value
    )
    ignore_attachments: bool = INTERACTIVE_APP_DEFAULTS.signal_ignore_attachments.value
    ignore_stories: bool = INTERACTIVE_APP_DEFAULTS.signal_ignore_stories.value
    ignore_avatars: bool = INTERACTIVE_APP_DEFAULTS.signal_ignore_avatars.value
    ignore_stickers: bool = INTERACTIVE_APP_DEFAULTS.signal_ignore_stickers.value
    send_read_receipts: bool = INTERACTIVE_APP_DEFAULTS.signal_send_read_receipts.value
    drop_pending_messages_on_startup: bool = (
        INTERACTIVE_APP_DEFAULTS.signal_drop_pending_messages_on_startup.value
    )
    text_mode: str = INTERACTIVE_APP_DEFAULTS.signal_text_mode.value

    @field_validator("base_url", mode="before")
    @classmethod
    def _normalize_base_url(cls, value: Any) -> str:
        normalized = str(value or "").strip()
        return (normalized or INTERACTIVE_APP_DEFAULTS.signal_base_url.value).rstrip(
            "/"
        )

    @field_validator("account", mode="before")
    @classmethod
    def _normalize_account(cls, value: Any) -> str:
        return str(value or "").strip()

    @field_validator(
        "allowed_sender_ids", "allowed_group_ids", "mention_markers", mode="before"
    )
    @classmethod
    def _normalize_signal_lists(cls, value: Any) -> list[str]:
        return _normalized_string_list(value)

    @field_validator("group_policy")
    @classmethod
    def _validate_group_policy(cls, value: str) -> str:
        normalized = (value or "").strip().lower()
        if normalized not in {"open", "mention"}:
            raise ValueError("Signal group_policy must be 'open' or 'mention'.")
        return normalized

    @field_validator(
        "poll_timeout_seconds",
        "poll_interval_seconds",
        "request_timeout_seconds",
        mode="before",
    )
    @classmethod
    def _normalize_positive_float(cls, value: Any, info: Any) -> float:
        try:
            parsed = float(value)
        except (TypeError, ValueError) as exc:
            raise ValueError(
                f"interactive_apps.plugins.builtin.signal.settings.{info.field_name} must be a number."
            ) from exc
        if parsed <= 0:
            raise ValueError(
                f"interactive_apps.plugins.builtin.signal.settings.{info.field_name} must be greater than 0."
            )
        return parsed

    @field_validator("max_messages_per_poll", mode="before")
    @classmethod
    def _normalize_max_messages_per_poll(cls, value: Any) -> int:
        try:
            parsed = int(value)
        except (TypeError, ValueError) as exc:
            raise ValueError(
                "interactive_apps.plugins.builtin.signal.settings.max_messages_per_poll must be an integer."
            ) from exc
        if parsed < 1:
            raise ValueError(
                "interactive_apps.plugins.builtin.signal.settings.max_messages_per_poll must be >= 1."
            )
        return parsed

    @field_validator("text_mode")
    @classmethod
    def _validate_text_mode(cls, value: str) -> str:
        normalized = (value or "").strip().lower()
        if normalized not in {"normal", "styled"}:
            raise ValueError("Signal text_mode must be 'normal' or 'styled'.")
        return normalized

    @model_validator(mode="after")
    def _validate_enabled_signal(self) -> "SignalAppSettings":
        if not self.enabled:
            return self
        if not self.base_url:
            raise ValueError(
                "interactive_apps.plugins.builtin.signal.settings.base_url is required when Signal is enabled."
            )
        if not self.account:
            raise ValueError(
                "interactive_apps.plugins.builtin.signal.settings.account is required when Signal is enabled."
            )
        if (
            self.allowed_group_ids
            and self.group_policy == "mention"
            and not self.mention_markers
        ):
            raise ValueError(
                "interactive_apps.plugins.builtin.signal.settings.mention_markers must include at least one marker when "
                "Signal groups use group_policy='mention'."
            )
        return self


class PhotonAppSettings(BaseModel):
    enabled: bool = False
    project_id: str = ""
    project_id_env_var: str = INTERACTIVE_APP_DEFAULTS.photon_project_id_env_var.value
    project_secret_env_var: str = (
        INTERACTIVE_APP_DEFAULTS.photon_project_secret_env_var.value
    )
    sidecar_token_env_var: str = (
        INTERACTIVE_APP_DEFAULTS.photon_sidecar_token_env_var.value
    )
    sidecar_bind_host: str = INTERACTIVE_APP_DEFAULTS.photon_sidecar_bind_host.value
    sidecar_port: int = INTERACTIVE_APP_DEFAULTS.photon_sidecar_port.value
    sidecar_autostart: bool = INTERACTIVE_APP_DEFAULTS.photon_sidecar_autostart.value
    node_bin: str = INTERACTIVE_APP_DEFAULTS.photon_node_bin.value
    allowed_sender_ids: list[str] = Field(default_factory=list)
    allowed_group_ids: list[str] = Field(default_factory=list)
    group_policy: str = INTERACTIVE_APP_DEFAULTS.photon_group_policy.value
    mention_markers: list[str] = Field(default_factory=list)
    request_timeout_seconds: float = (
        INTERACTIVE_APP_DEFAULTS.photon_request_timeout_seconds.value
    )
    max_inline_attachment_bytes: int = (
        INTERACTIVE_APP_DEFAULTS.photon_max_inline_attachment_bytes.value
    )
    markdown: bool = INTERACTIVE_APP_DEFAULTS.photon_markdown.value
    telemetry: bool = INTERACTIVE_APP_DEFAULTS.photon_telemetry.value

    @field_validator(
        "project_id",
        "project_id_env_var",
        "project_secret_env_var",
        "sidecar_token_env_var",
        "sidecar_bind_host",
        "node_bin",
        mode="before",
    )
    @classmethod
    def _normalize_string(cls, value: Any, info: Any) -> str:
        normalized = str(value or "").strip()
        if info.field_name == "project_id":
            return normalized
        default_name = f"photon_{info.field_name}"
        default_value = getattr(INTERACTIVE_APP_DEFAULTS, default_name).value
        return normalized or str(default_value)

    @field_validator(
        "allowed_sender_ids", "allowed_group_ids", "mention_markers", mode="before"
    )
    @classmethod
    def _normalize_photon_lists(cls, value: Any) -> list[str]:
        return _normalized_string_list(value)

    @field_validator("group_policy")
    @classmethod
    def _validate_group_policy(cls, value: str) -> str:
        normalized = (value or "").strip().lower()
        if normalized not in {"open", "mention"}:
            raise ValueError("Photon group_policy must be 'open' or 'mention'.")
        return normalized

    @field_validator("sidecar_port", mode="before")
    @classmethod
    def _normalize_sidecar_port(cls, value: Any) -> int:
        try:
            parsed = int(value or INTERACTIVE_APP_DEFAULTS.photon_sidecar_port.value)
        except (TypeError, ValueError) as exc:
            raise ValueError(
                "interactive_apps.plugins.builtin.photon.settings.sidecar_port must be an integer."
            ) from exc
        if parsed < 1 or parsed > 65_535:
            raise ValueError(
                "interactive_apps.plugins.builtin.photon.settings.sidecar_port must be between 1 and 65535."
            )
        return parsed

    @field_validator("request_timeout_seconds", mode="before")
    @classmethod
    def _normalize_request_timeout(cls, value: Any) -> float:
        try:
            raw = (
                INTERACTIVE_APP_DEFAULTS.photon_request_timeout_seconds.value
                if value in (None, "")
                else value
            )
            parsed = float(raw)
        except (TypeError, ValueError) as exc:
            raise ValueError(
                "interactive_apps.plugins.builtin.photon.settings.request_timeout_seconds must be a number."
            ) from exc
        if parsed <= 0:
            raise ValueError(
                "interactive_apps.plugins.builtin.photon.settings.request_timeout_seconds must be greater than 0."
            )
        return parsed

    @field_validator("max_inline_attachment_bytes", mode="before")
    @classmethod
    def _normalize_max_inline_attachment_bytes(cls, value: Any) -> int:
        try:
            raw = (
                INTERACTIVE_APP_DEFAULTS.photon_max_inline_attachment_bytes.value
                if value in (None, "")
                else value
            )
            parsed = int(raw)
        except (TypeError, ValueError) as exc:
            raise ValueError(
                "interactive_apps.plugins.builtin.photon.settings.max_inline_attachment_bytes must be an integer."
            ) from exc
        if parsed < 1:
            raise ValueError(
                "interactive_apps.plugins.builtin.photon.settings.max_inline_attachment_bytes must be >= 1."
            )
        return parsed

    @model_validator(mode="after")
    def _validate_enabled_photon(self) -> "PhotonAppSettings":
        if not self.enabled:
            return self
        if not self.project_id and not self.project_id_env_var:
            raise ValueError(
                "interactive_apps.plugins.builtin.photon.settings.project_id or project_id_env_var is required when Photon is enabled."
            )
        if not self.project_secret_env_var:
            raise ValueError(
                "interactive_apps.plugins.builtin.photon.settings.project_secret_env_var is required when Photon is enabled."
            )
        if (
            self.allowed_group_ids
            and self.group_policy == "mention"
            and not self.mention_markers
        ):
            raise ValueError(
                "interactive_apps.plugins.builtin.photon.settings.mention_markers must include at least one marker when "
                "Photon groups use group_policy='mention'."
            )
        return self


class WebChatAttachmentSettings(BaseModel):
    enabled: bool = INTERACTIVE_APP_DEFAULTS.internal_website_attachment_enabled.value
    max_file_bytes: int = (
        INTERACTIVE_APP_DEFAULTS.internal_website_attachment_max_file_bytes.value
    )
    # Uploads from every chat app use this block's limits and retention
    # (assistant_gateway.upload_retention deletes older ones; 0 keeps them).
    retention_days: int = Field(
        default=INTERACTIVE_APP_DEFAULTS.internal_website_attachment_retention_days.value,
        ge=0,
    )
    allowed_kinds: list[str] = Field(
        default_factory=lambda: list(
            INTERACTIVE_APP_DEFAULTS.internal_website_attachment_allowed_kinds.value
        )
    )
    nats_object_bucket: str = (
        INTERACTIVE_APP_DEFAULTS.internal_website_attachment_nats_object_bucket.value
    )
    nats_object_ttl_seconds: int = INTERACTIVE_APP_DEFAULTS.internal_website_attachment_nats_object_ttl_seconds.value
    nats_object_max_bytes: Optional[int] = (
        INTERACTIVE_APP_DEFAULTS.internal_website_attachment_nats_object_max_bytes.value
    )
    remote_copy_enabled: bool = (
        INTERACTIVE_APP_DEFAULTS.internal_website_attachment_remote_copy_enabled.value
    )

    @field_validator("max_file_bytes", "nats_object_ttl_seconds", mode="before")
    @classmethod
    def _normalize_positive_int(cls, value: Any, info: Any) -> int:
        try:
            parsed = int(value)
        except (TypeError, ValueError) as exc:
            raise ValueError(
                f"interactive_apps.plugins.builtin.internal_website.settings.attachments.{info.field_name} must be an integer."
            ) from exc
        if parsed < 1:
            raise ValueError(
                f"interactive_apps.plugins.builtin.internal_website.settings.attachments.{info.field_name} must be >= 1."
            )
        return parsed

    @field_validator("nats_object_max_bytes", mode="before")
    @classmethod
    def _normalize_optional_positive_int(cls, value: Any) -> Optional[int]:
        if value in (None, ""):
            return None
        try:
            parsed = int(value)
        except (TypeError, ValueError) as exc:
            raise ValueError(
                "interactive_apps.plugins.builtin.internal_website.settings.attachments.nats_object_max_bytes must be an integer."
            ) from exc
        if parsed < 1:
            raise ValueError(
                "interactive_apps.plugins.builtin.internal_website.settings.attachments.nats_object_max_bytes must be >= 1."
            )
        return parsed

    @field_validator("nats_object_bucket", mode="before")
    @classmethod
    def _normalize_required_string(cls, value: Any, info: Any) -> str:
        normalized = str(value or "").strip()
        if not normalized:
            default_value = getattr(
                INTERACTIVE_APP_DEFAULTS,
                f"internal_website_attachment_{info.field_name}",
            ).value
            return str(default_value)
        return normalized

    @field_validator("allowed_kinds", mode="before")
    @classmethod
    def _normalize_allowed_kinds(cls, value: Any) -> list[str]:
        if isinstance(value, str):
            candidates = [item.strip() for item in value.split(",")]
        elif isinstance(value, (list, tuple, set)):
            candidates = [str(item).strip() for item in value]
        else:
            candidates = []
        allowed = []
        valid = {"image", "document", "audio", "video"}
        for candidate in (
            candidates
            or INTERACTIVE_APP_DEFAULTS.internal_website_attachment_allowed_kinds.value
        ):
            if candidate not in valid:
                raise ValueError(
                    "interactive_apps.plugins.builtin.internal_website.settings.attachments.allowed_kinds entries "
                    "must be one of image, document, audio, video."
                )
            if candidate not in allowed:
                allowed.append(candidate)
        return allowed


class InternalWebsiteAppSettings(BaseModel):
    enabled: bool = INTERACTIVE_APP_DEFAULTS.internal_website_enabled.value
    bind_host: str = INTERACTIVE_APP_DEFAULTS.internal_website_bind_host.value
    bind_port: int = INTERACTIVE_APP_DEFAULTS.internal_website_bind_port.value
    public_port: Optional[int] = (
        INTERACTIVE_APP_DEFAULTS.internal_website_public_port.value
    )
    docs_root: Optional[str] = INTERACTIVE_APP_DEFAULTS.internal_website_docs_root.value
    skills_dir: Optional[str] = (
        INTERACTIVE_APP_DEFAULTS.internal_website_skills_dir.value
    )
    web_chat_enabled: bool = (
        INTERACTIVE_APP_DEFAULTS.internal_website_web_chat_enabled.value
    )
    web_chat_token_env_var: str = (
        INTERACTIVE_APP_DEFAULTS.internal_website_web_chat_token_env_var.value
    )
    attachments: WebChatAttachmentSettings = Field(
        default_factory=WebChatAttachmentSettings
    )

    @field_validator("bind_host", mode="before")
    @classmethod
    def _normalize_bind_host(cls, value: Any) -> str:
        normalized = str(value or "").strip()
        return normalized or INTERACTIVE_APP_DEFAULTS.internal_website_bind_host.value

    @field_validator("bind_port", mode="before")
    @classmethod
    def _normalize_bind_port(cls, value: Any) -> int:
        try:
            parsed = int(
                value or INTERACTIVE_APP_DEFAULTS.internal_website_bind_port.value
            )
        except (TypeError, ValueError) as exc:
            raise ValueError(
                "interactive_apps.plugins.builtin.internal_website.settings.bind_port must be an integer."
            ) from exc
        if parsed < 1 or parsed > 65_535:
            raise ValueError(
                "interactive_apps.plugins.builtin.internal_website.settings.bind_port must be between 1 and 65535."
            )
        return parsed

    @field_validator("public_port", mode="before")
    @classmethod
    def _normalize_public_port(cls, value: Any) -> Optional[int]:
        if value in (None, ""):
            return None
        try:
            parsed = int(value)
        except (TypeError, ValueError) as exc:
            raise ValueError(
                "interactive_apps.plugins.builtin.internal_website.settings.public_port must be an integer."
            ) from exc
        if parsed < 1 or parsed > 65_535:
            raise ValueError(
                "interactive_apps.plugins.builtin.internal_website.settings.public_port must be between 1 and 65535."
            )
        return parsed

    @field_validator("docs_root", "skills_dir", mode="before")
    @classmethod
    def _normalize_optional_string(cls, value: Any) -> Optional[str]:
        return _normalized_optional_string(value)

    @field_validator("web_chat_token_env_var", mode="before")
    @classmethod
    def _normalize_web_chat_token_env_var(cls, value: Any) -> str:
        normalized = str(value or "").strip()
        return (
            normalized
            or INTERACTIVE_APP_DEFAULTS.internal_website_web_chat_token_env_var.value
        )


class ExternalInteractiveAppSettings(BaseModel):
    """Host-owned portion of settings for an allowlisted package plugin."""

    model_config = ConfigDict(extra="allow")

    enabled: bool = False


INTERACTIVE_APP_EXECUTION_IN_PROCESS = "in_process"


INTERACTIVE_APP_EXECUTION_ADAPTER_HOST = "adapter_host"


ADAPTER_HOST_TRANSPORT_UNIX = "unix"


ADAPTER_HOST_TRANSPORT_MTLS = "mtls"


# Default variable holding the adapter host's bearer token.
ADAPTER_HOST_TOKEN_ENV_VAR = "ADAPTER_HOST_AUTH_TOKEN"


class AdapterHostProtocolLimitsSettings(BaseModel):
    """Gateway ceilings negotiated independently for each registration epoch."""

    model_config = ConfigDict(extra="forbid")

    max_in_flight_events: int = Field(default=16, ge=1, le=10_000)
    max_payload_bytes: int = Field(
        default=2 * 1024 * 1024, ge=1_024, le=256 * 1024 * 1024
    )
    max_attachment_bytes: int = Field(
        default=30 * 1024 * 1024, ge=0, le=256 * 1024 * 1024
    )
    max_pending_events: int = Field(default=1_000, ge=1, le=1_000_000)
    heartbeat_interval_seconds: float = Field(default=15.0, ge=1.0, le=3_600.0)
    acknowledgement_timeout_seconds: float = Field(default=60.0, ge=1.0, le=86_400.0)
    retention_seconds: int = Field(default=604_800, ge=60, le=31_536_000)


class AdapterHostTLSSettings(BaseModel):
    """Mutual-TLS material for container or remote private transports."""

    model_config = ConfigDict(extra="forbid")

    server_certificate_path: str = ""
    server_private_key_path: str = ""
    client_ca_path: str = ""
    client_certificate_path: str = ""
    client_private_key_path: str = ""
    server_ca_path: str = ""
    server_name: str = ""

    @field_validator(
        "server_certificate_path",
        "server_private_key_path",
        "client_ca_path",
        "client_certificate_path",
        "client_private_key_path",
        "server_ca_path",
        mode="before",
    )
    @classmethod
    def _normalize_path(cls, value: Any) -> str:
        rendered = str(value or "").strip()
        if rendered and ".." in Path(rendered).parts:
            raise ValueError("Adapter-host TLS paths must not contain '..'.")
        return rendered

    @field_validator("server_name", mode="before")
    @classmethod
    def _normalize_server_name(cls, value: Any) -> str:
        return str(value or "").strip()


class AdapterHostSettings(BaseModel):
    """Private authenticated transport and durable host-runtime settings."""

    model_config = ConfigDict(extra="forbid")

    enabled: bool = False
    transport: str = ADAPTER_HOST_TRANSPORT_UNIX
    host_id: str = "python-adapter-host"
    allowed_host_ids: list[str] = Field(default_factory=lambda: ["python-adapter-host"])
    token_env_var: str = ADAPTER_HOST_TOKEN_ENV_VAR
    unix_socket_path: str = "runtime/adapter-host.sock"
    unix_socket_mode: str = "0600"
    bind_host: str = "127.0.0.1"
    bind_port: int = Field(default=18700, ge=1, le=65535)
    connect_host: str = "127.0.0.1"
    tls: AdapterHostTLSSettings = Field(default_factory=AdapterHostTLSSettings)
    limits: AdapterHostProtocolLimitsSettings = Field(
        default_factory=AdapterHostProtocolLimitsSettings
    )
    lease_seconds: int = Field(default=45, ge=5, le=86_400)
    upload_ticket_ttl_seconds: int = Field(default=300, ge=10, le=86_400)
    upload_dir: str = "runtime/adapter-host-uploads"
    client_state_path: str = "dbs/adapter_host_client.db"
    reconnect_initial_delay_seconds: float = Field(default=0.5, ge=0.05, le=60.0)
    reconnect_max_delay_seconds: float = Field(default=30.0, ge=0.1, le=600.0)

    @field_validator("transport", mode="before")
    @classmethod
    def _normalize_transport(cls, value: Any) -> str:
        normalized = str(value or ADAPTER_HOST_TRANSPORT_UNIX).strip().lower()
        if normalized not in {ADAPTER_HOST_TRANSPORT_UNIX, ADAPTER_HOST_TRANSPORT_MTLS}:
            raise ValueError(
                "interactive_apps.adapter_host.transport must be 'unix' or 'mtls'."
            )
        return normalized

    @field_validator(
        "host_id", "token_env_var", "bind_host", "connect_host", mode="before"
    )
    @classmethod
    def _normalize_required_identifier(cls, value: Any) -> str:
        normalized = str(value or "").strip()
        if not normalized:
            raise ValueError("Adapter-host identity settings must not be empty.")
        return normalized

    @field_validator("allowed_host_ids", mode="before")
    @classmethod
    def _normalize_allowed_host_ids(cls, value: Any) -> list[str]:
        rendered = [str(item or "").strip() for item in (value or [])]
        normalized = list(dict.fromkeys(item for item in rendered if item))
        if not normalized:
            raise ValueError(
                "interactive_apps.adapter_host.allowed_host_ids must not be empty."
            )
        return normalized

    @field_validator(
        "unix_socket_path", "upload_dir", "client_state_path", mode="before"
    )
    @classmethod
    def _normalize_instance_path(cls, value: Any) -> str:
        rendered = str(value or "").strip()
        if not rendered or ".." in Path(rendered).parts:
            raise ValueError(
                "Adapter-host paths must be non-empty and must not contain '..'."
            )
        return rendered

    @field_validator("unix_socket_mode", mode="before")
    @classmethod
    def _normalize_socket_mode(cls, value: Any) -> str:
        rendered = str(value or "").strip()
        if not re.fullmatch(r"0?[0-7]{3}", rendered):
            raise ValueError(
                "interactive_apps.adapter_host.unix_socket_mode must be an octal mode such as '0600'."
            )
        return rendered[-3:]

    @model_validator(mode="after")
    def _validate_transport_material(self) -> "AdapterHostSettings":
        if self.reconnect_max_delay_seconds < self.reconnect_initial_delay_seconds:
            raise ValueError(
                "adapter-host reconnect_max_delay_seconds must be greater than or equal to reconnect_initial_delay_seconds"
            )
        if self.lease_seconds < self.limits.heartbeat_interval_seconds * 2:
            raise ValueError(
                "adapter-host lease_seconds must be at least twice limits.heartbeat_interval_seconds"
            )
        if self.enabled and self.transport == ADAPTER_HOST_TRANSPORT_MTLS:
            required = {
                "server_certificate_path": self.tls.server_certificate_path,
                "server_private_key_path": self.tls.server_private_key_path,
                "client_ca_path": self.tls.client_ca_path,
                "client_certificate_path": self.tls.client_certificate_path,
                "client_private_key_path": self.tls.client_private_key_path,
                "server_ca_path": self.tls.server_ca_path,
                "server_name": self.tls.server_name,
            }
            missing = sorted(name for name, item in required.items() if not item)
            if missing:
                raise ValueError(
                    "enabled mTLS adapter-host transport requires: "
                    + ", ".join(missing)
                )
        return self


class InteractiveAppPluginConfiguration(BaseModel):
    """One durable, normalized plugin selection in instance configuration."""

    distribution: Optional[str] = None
    execution: str = INTERACTIVE_APP_EXECUTION_IN_PROCESS
    adapter_instance_id: Optional[str] = None
    settings: object = Field(default_factory=dict)

    @field_validator("distribution", mode="before")
    @classmethod
    def _normalize_distribution(cls, value: Any) -> Optional[str]:
        if value in (None, ""):
            return None
        return normalize_distribution_name(value)

    @field_validator("execution", mode="before")
    @classmethod
    def _normalize_execution(cls, value: Any) -> str:
        normalized = str(value or INTERACTIVE_APP_EXECUTION_IN_PROCESS).strip().lower()
        if normalized not in {
            INTERACTIVE_APP_EXECUTION_IN_PROCESS,
            INTERACTIVE_APP_EXECUTION_ADAPTER_HOST,
        }:
            raise ValueError(
                "Interactive app execution must be 'in_process' or 'adapter_host'."
            )
        return normalized

    @field_validator("adapter_instance_id", mode="before")
    @classmethod
    def _normalize_adapter_instance_id(cls, value: Any) -> Optional[str]:
        normalized = str(value or "").strip()
        if not normalized:
            return None
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:/-]{0,255}", normalized):
            raise ValueError("adapter_instance_id contains unsupported characters.")
        return normalized

    @field_validator("settings", mode="before")
    @classmethod
    def _validate_settings_shape(cls, value: Any) -> object:
        if isinstance(value, BaseModel):
            return value
        if not isinstance(value, Mapping):
            raise ValueError("Interactive app plugin settings must be a mapping.")
        return dict(value)

    @property
    def enabled(self) -> bool:
        return bool(getattr(self.settings, "enabled", False))


_BUILTIN_INTERACTIVE_APP_CONFIGURATION_MODELS: dict[str, type[BaseModel]] = {
    BUILTIN_DISCORD_PLUGIN_ID: DiscordAppSettings,
    BUILTIN_TELEGRAM_PLUGIN_ID: TelegramAppSettings,
    BUILTIN_WHATSAPP_PLUGIN_ID: WhatsAppAppSettings,
    BUILTIN_SIGNAL_PLUGIN_ID: SignalAppSettings,
    BUILTIN_PHOTON_PLUGIN_ID: PhotonAppSettings,
    BUILTIN_INTERNAL_WEBSITE_PLUGIN_ID: InternalWebsiteAppSettings,
}


def _default_interactive_app_plugins() -> dict[str, InteractiveAppPluginConfiguration]:
    return {
        plugin_id: InteractiveAppPluginConfiguration(settings=model())
        for plugin_id, model in _BUILTIN_INTERACTIVE_APP_CONFIGURATION_MODELS.items()
    }


class InteractiveAppsSettings(BaseModel):
    """Registry-keyed adapter configuration and optional-package allowlist."""

    model_config = ConfigDict(extra="forbid")

    plugins: dict[str, InteractiveAppPluginConfiguration] = Field(
        default_factory=_default_interactive_app_plugins
    )
    adapter_host: AdapterHostSettings = Field(default_factory=AdapterHostSettings)

    @model_validator(mode="before")
    @classmethod
    def _reject_superseded_adapter_blocks(cls, value: Any) -> Any:
        if not isinstance(value, Mapping):
            return value
        removed = sorted(
            set(value).intersection(
                {
                    "website",
                    "internal_website",
                    "discord",
                    "telegram",
                    "whatsapp",
                    "signal",
                    "photon",
                }
            )
        )
        if removed:
            joined = ", ".join(f"interactive_apps.{name}" for name in removed)
            raise ValueError(
                f"{joined} has been removed. Configure adapters under "
                "interactive_apps.plugins.<plugin-id>.settings instead."
            )
        return value

    @model_validator(mode="after")
    def _validate_and_bind_plugins(self) -> "InteractiveAppsSettings":
        normalized_plugins: dict[str, InteractiveAppPluginConfiguration] = {}
        for raw_plugin_id, entry in self.plugins.items():
            plugin_id = normalize_interactive_app_plugin_id(raw_plugin_id)
            if plugin_id != raw_plugin_id:
                raise ValueError(
                    f"Interactive app plugin id {raw_plugin_id!r} is not canonical; "
                    f"use {plugin_id!r}."
                )
            if plugin_id in normalized_plugins:
                raise ValueError(
                    f"Interactive app plugin id {plugin_id!r} is duplicated."
                )

            configuration_model = _BUILTIN_INTERACTIVE_APP_CONFIGURATION_MODELS.get(
                plugin_id
            )
            if configuration_model is not None:
                if entry.distribution is not None:
                    raise ValueError(
                        f"Bundled interactive app plugin {plugin_id!r} must not set distribution."
                    )
                if not isinstance(entry.settings, configuration_model):
                    entry.settings = configuration_model.model_validate(entry.settings)
            else:
                if plugin_id.startswith("builtin."):
                    raise ValueError(
                        f"Unknown bundled interactive app plugin id {plugin_id!r}."
                    )
                if entry.distribution is None:
                    raise ValueError(
                        f"Optional interactive app plugin {plugin_id!r} must set distribution."
                    )
                if not isinstance(entry.settings, ExternalInteractiveAppSettings):
                    entry.settings = ExternalInteractiveAppSettings.model_validate(
                        entry.settings
                    )
            normalized_plugins[plugin_id] = entry

        for (
            plugin_id,
            configuration_model,
        ) in _BUILTIN_INTERACTIVE_APP_CONFIGURATION_MODELS.items():
            normalized_plugins.setdefault(
                plugin_id,
                InteractiveAppPluginConfiguration(settings=configuration_model()),
            )
        self.plugins = {
            plugin_id: normalized_plugins[plugin_id]
            for plugin_id in (
                *BUILTIN_INTERACTIVE_APP_PLUGIN_IDS,
                *sorted(
                    set(normalized_plugins) - set(BUILTIN_INTERACTIVE_APP_PLUGIN_IDS)
                ),
            )
        }
        external_plugins = [
            plugin_id
            for plugin_id, entry in self.plugins.items()
            if entry.enabled
            and entry.execution == INTERACTIVE_APP_EXECUTION_ADAPTER_HOST
        ]
        if external_plugins and not self.adapter_host.enabled:
            raise ValueError(
                "interactive_apps.adapter_host.enabled must be true when an enabled plugin uses execution=adapter_host."
            )
        if self.adapter_host.enabled and not external_plugins:
            raise ValueError(
                "interactive_apps.adapter_host.enabled requires at least one enabled plugin with execution=adapter_host."
            )
        if BUILTIN_INTERNAL_WEBSITE_PLUGIN_ID in external_plugins:
            raise ValueError(
                "The internal Website is a gateway-owned operator surface and must use execution=in_process."
            )
        if (
            any(
                entry.enabled
                and entry.execution == INTERACTIVE_APP_EXECUTION_ADAPTER_HOST
                for entry in self.plugins.values()
            )
            and self.adapter_host.host_id not in self.adapter_host.allowed_host_ids
        ):
            raise ValueError(
                "interactive_apps.adapter_host.host_id must be present in allowed_host_ids."
            )
        return self

    def resolved_adapter_instance_id(self, plugin_id: str, instance_name: str) -> str:
        entry = self.require_plugin(plugin_id)
        return entry.adapter_instance_id or f"{instance_name}:{plugin_id}"

    def plugin(self, plugin_id: str) -> InteractiveAppPluginConfiguration | None:
        return self.plugins.get(normalize_interactive_app_plugin_id(plugin_id))

    def require_plugin(self, plugin_id: str) -> InteractiveAppPluginConfiguration:
        entry = self.plugin(plugin_id)
        if entry is None:
            raise KeyError(f"Interactive app plugin {plugin_id!r} is not configured.")
        return entry

    @property
    def optional_plugin_ids(self) -> tuple[str, ...]:
        return tuple(
            plugin_id
            for plugin_id in self.plugins
            if plugin_id not in BUILTIN_INTERACTIVE_APP_PLUGIN_IDS
        )

    def _builtin_settings(self, plugin_id: str, model: type[BaseModel]) -> BaseModel:
        settings = self.require_plugin(plugin_id).settings
        if not isinstance(settings, model):
            raise TypeError(
                f"Interactive app plugin {plugin_id!r} has unexpected settings type "
                f"{type(settings).__name__!r}."
            )
        return settings

    @property
    def discord(self) -> DiscordAppSettings:
        return self._builtin_settings(  # type: ignore[return-value]
            BUILTIN_DISCORD_PLUGIN_ID, DiscordAppSettings
        )

    @discord.setter
    def discord(self, value: DiscordAppSettings) -> None:
        self.require_plugin(BUILTIN_DISCORD_PLUGIN_ID).settings = value

    @property
    def telegram(self) -> TelegramAppSettings:
        return self._builtin_settings(  # type: ignore[return-value]
            BUILTIN_TELEGRAM_PLUGIN_ID, TelegramAppSettings
        )

    @telegram.setter
    def telegram(self, value: TelegramAppSettings) -> None:
        self.require_plugin(BUILTIN_TELEGRAM_PLUGIN_ID).settings = value

    @property
    def whatsapp(self) -> WhatsAppAppSettings:
        return self._builtin_settings(  # type: ignore[return-value]
            BUILTIN_WHATSAPP_PLUGIN_ID, WhatsAppAppSettings
        )

    @whatsapp.setter
    def whatsapp(self, value: WhatsAppAppSettings) -> None:
        self.require_plugin(BUILTIN_WHATSAPP_PLUGIN_ID).settings = value

    @property
    def signal(self) -> SignalAppSettings:
        return self._builtin_settings(  # type: ignore[return-value]
            BUILTIN_SIGNAL_PLUGIN_ID, SignalAppSettings
        )

    @signal.setter
    def signal(self, value: SignalAppSettings) -> None:
        self.require_plugin(BUILTIN_SIGNAL_PLUGIN_ID).settings = value

    @property
    def photon(self) -> PhotonAppSettings:
        return self._builtin_settings(  # type: ignore[return-value]
            BUILTIN_PHOTON_PLUGIN_ID, PhotonAppSettings
        )

    @photon.setter
    def photon(self, value: PhotonAppSettings) -> None:
        self.require_plugin(BUILTIN_PHOTON_PLUGIN_ID).settings = value

    @property
    def internal_website(self) -> InternalWebsiteAppSettings:
        return self._builtin_settings(  # type: ignore[return-value]
            BUILTIN_INTERNAL_WEBSITE_PLUGIN_ID, InternalWebsiteAppSettings
        )

    @internal_website.setter
    def internal_website(self, value: InternalWebsiteAppSettings) -> None:
        self.require_plugin(BUILTIN_INTERNAL_WEBSITE_PLUGIN_ID).settings = value
