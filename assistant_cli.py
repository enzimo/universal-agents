#!/usr/bin/env python3
"""CLI for gateway onboarding, chat, and manager-owned schedule management."""

from __future__ import annotations

from assistant_gateway.runtime_config import build_collector_runtime_settings

import argparse
import asyncio
import copy
import json
import logging
import os
from pathlib import Path, PurePosixPath
import sys
import webbrowser
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Optional, TextIO

import yaml

from assistant_gateway.bootstrap import bootstrap_instance, seed_default_gateway_user
from activity_collector.service import run_activity_collector
from assistant_gateway.channel_bridge import ChannelBridge
from runtime_settings.common import DEFAULT_TELEGRAM_MANAGER_OFFLINE_POLICY, TELEGRAM_MANAGER_OFFLINE_POLICY_NOTIFY, TELEGRAM_MANAGER_OFFLINE_POLICY_PAUSE_POLLING
from assistant_gateway.config import GatewayInstanceConfig, inspect_instance_config_version, migrate_instance_config_file
from runtime_settings.network import reject_removed_public_url_env_vars
from runtime_settings.secrets_source import SECRETS_SOURCE_ENVIRONMENT
from runtime_settings.security import SECURITY_LAYER_MODES, normalize_security_layer_mode
from default_values import (
    EMBEDDING_DEFAULTS,
    GATEWAY_DEFAULTS,
    HEARTBEAT_DEFAULTS,
    INTERACTIVE_APP_DEFAULTS,
    LLM_DEFAULTS,
    NETWORK_DEFAULTS,
)
from secure_tool_broker.defaults import (
    DEFAULT_BIND_PORT as DEFAULT_SECURE_TOOL_BROKER_BIND_PORT,
    DEFAULT_INTERNAL_BASE_URL as DEFAULT_SECURE_TOOL_BROKER_INTERNAL_BASE_URL,
    resolve_secure_tool_broker_internal_base_url,
)
from assistant_gateway.application.gateway_application import (
    GatewayApplication,
    GatewayApplicationResponse,
    GatewayDeliveryEvent,
)
from assistant_gateway.gateway import UserGateway
from assistant_gateway.application.interactive_commands import parse_reply_command
from assistant_gateway.interactive_apps import InteractiveAppManager
from assistant_gateway.interactive_apps.builtin_registry import (
    build_interactive_app_registry,
)
from assistant_gateway.interactive_apps.catalog import build_interactive_app_catalog
from assistant_gateway.adapter_host.composition import GatewayAdapterHostRuntime
from assistant_gateway.adapter_host.remote_host import (
    build_python_adapter_host_runtime,
)
from assistant_gateway.application.command_help import build_interactive_session_help
from assistant_gateway.instance_secrets import (
    environment_secret_values,
    ensure_instance_sealbox_material,
    ensure_internal_credential_env_file,
    missing_secrets,
    missing_secrets_text,
)
from assistant_gateway.onboarding_engine import (
    OnboardingTemplate,
    apply_onboarding_template,
    default_instance_root,
    default_launch_env_file,
    launch_env_file_for,
    load_base_onboarding_template,
    resolve_onboarding_model_id,
)
from assistant_gateway.onboarding_web.settings_import import looks_like_instance_config
from assistant_gateway.onboarding_reconciliation import (
    OnboardingReport,
    diff_config_paths,
    diff_env_keys,
    onboarding_report_detail_path,
    read_yaml_mapping,
    write_onboarding_report,
)
from assistant_gateway.translation import ChannelInboundMessage
from custom_agents.memory_graph_embedding_profile_runtime import (
    build_embedding_profile_spec,
    inspect_memory_graph_embedding_profile,
)
from custom_agents.durable_owner_scope import manager_durable_scope_id
from runtime_settings.model_request_options import (
    LITELLM_API_MODES,
    REASONING_SUMMARY_VALUES,
    normalize_litellm_api_mode,
    normalize_reasoning_summary,
)
from env_secrets import materialize_secret_file_envs
from internal_credentials import (
    merge_env_file,
    parse_env_file,
)
from secure_tool_broker.credential_state import configure_broker_credential_environment
from secure_tool_broker.mcp_config import BrokerMCPConfig
from secure_tool_broker.sealbox_runtime import (
    apply_sealbox_cli_env,
    resolve_sealbox_runtime_config,
)
from secure_tool_broker.service import (
    resolve_bind_from_base_url,
    run_secure_tool_broker,
)
from secure_tool_broker.auth_pages import BROKER_APPEARANCE_ENV
from storage.memory_graph_repository import create_memory_graph_repository
from storage.objective_repository import create_objective_repository

try:
    from dotenv import load_dotenv
except ImportError:
    load_dotenv = None

try:
    import readline
except ImportError:
    readline = None


logging.basicConfig(
    level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger(__name__)

INTERACTIVE_SESSION_HELP = build_interactive_session_help(include_exit=True)
HEADLESS_STARTUP_NOTICE_ENV = "TEAM_HEADLESS_STARTUP_NOTICE"
BROKER_CREDENTIAL_STATE_ENV_KEYS = (
    BROKER_APPEARANCE_ENV,
    "SECURE_TOOL_BROKER_CREDENTIALS_DIR",
    "GH_CONFIG_DIR",
    "GITHUB_CONFIG_DIR",
    "GOG_KEYRING_PASSWORD",
    "GOG_KEYRING_PASSWORD_FILE",
    "BITWARDENCLI_APPDATA_DIR",
    "BW_SESSION_FILE",
    "BITWARDEN_SERVER_URL",
    "BW_SERVER_URL",
    "OP_CONFIG_DIR",
    "OP_SERVICE_ACCOUNT_TOKEN_FILE",
    "KEEPASSXC_PASSWORD_FILE",
    "KEEPASS_PASSWORD_FILE",
    "VAULT_TOKEN_FILE",
    "SEALBOX_URL",
    "SEALBOX_TOKEN",
    "SEALBOX_TOKEN_FILE",
    "SEALBOX_PUBLIC_KEY",
    "SEALBOX_PUBLIC_KEY_FILE",
    "SEALBOX_PRIVATE_KEY",
    "SEALBOX_PRIVATE_KEY_FILE",
    "SEALBOX_ROOT_TOKEN_FILE",
    "SECURE_TOOL_BROKER_SEALBOX_TENANT_MATERIAL_DIR",
    "SEALBOX_API_VERSION",
    "SEALBOX_OUTPUT_FORMAT",
    "SECURE_TOOL_BROKER_CREDENTIAL_STATE_BACKEND_MODE",
    "SECURE_TOOL_BROKER_CREDENTIAL_STATE_BACKEND_SOURCE",
    "SECURE_TOOL_BROKER_CREDENTIAL_STATE_BACKEND_NAMESPACE",
    "SECURE_TOOL_BROKER_CREDENTIAL_STATE_BACKEND_FALLBACK_TO_FILES",
)
@dataclass
class CliMessageRef:
    ref_id: str
    session_key: Optional[str]
    conversation_id: Optional[str]
    text: str


@dataclass(frozen=True)
class GatewayOnboardingSeed:
    """What `onboard` starts from: the base config's template and an optional env file.

    The template is the one the web onboarding uses too
    (assistant_gateway.onboarding_engine.OnboardingTemplate).
    """

    template: OnboardingTemplate
    source_env_file_path: Optional[str]


def _normalize_telegram_manager_offline_policy(value: Optional[str]) -> str:
    normalized = str(value or "").strip().lower()
    if normalized in {
        TELEGRAM_MANAGER_OFFLINE_POLICY_NOTIFY,
        TELEGRAM_MANAGER_OFFLINE_POLICY_PAUSE_POLLING,
    }:
        return normalized
    raise SystemExit(
        "Telegram manager-offline policy must be 'notify' or 'pause_telegram_polling'."
    )


class CliMessageTracker:
    def __init__(self) -> None:
        self._counter = 0
        self._refs: dict[str, CliMessageRef] = {}

    def add(
        self,
        *,
        text: str,
        session_key: Optional[str],
        conversation_id: Optional[str],
    ) -> CliMessageRef:
        self._counter += 1
        ref_id = f"msg_{self._counter}"
        ref = CliMessageRef(
            ref_id=ref_id,
            session_key=session_key,
            conversation_id=conversation_id,
            text=text,
        )
        self._refs[ref_id] = ref
        return ref

    def get(self, ref_id: str) -> Optional[CliMessageRef]:
        return self._refs.get(ref_id)


def _format_cli_timestamp(now: Optional[datetime] = None) -> str:
    current = now or datetime.now().astimezone()
    return current.astimezone().strftime("%H:%M")


class InteractiveCliConsole:
    def __init__(
        self,
        *,
        prompt: str = "> ",
        stream: Optional[TextIO] = None,
        now_factory: Optional[Callable[[], datetime]] = None,
    ) -> None:
        self.default_prompt = prompt
        self.stream = stream or sys.stdout
        self.now_factory = now_factory or (lambda: datetime.now().astimezone())
        self._input_active = False
        self._active_prompt = prompt

    async def read_input(self, prompt: Optional[str] = None) -> str:
        active_prompt = prompt or self.default_prompt
        self._input_active = True
        self._active_prompt = active_prompt
        try:
            return await asyncio.to_thread(input, active_prompt)
        finally:
            self._input_active = False
            self._active_prompt = self.default_prompt

    def print_message(
        self,
        *,
        ref_id: str,
        conversation_id: Optional[str],
        text: str,
    ) -> None:
        rendered = (
            f"[{_format_cli_timestamp(self.now_factory())}] "
            f"[{ref_id}] ({conversation_id or '-'}) {text}"
        )
        self._write_line(rendered)

    def print_text(self, text: str) -> None:
        self._write_line(text)

    def _write_line(self, text: str) -> None:
        if self._input_active and getattr(self.stream, "isatty", lambda: False)():
            buffer = self._current_input_buffer()
            self.stream.write("\r\033[2K")
            self.stream.write(f"{text}\n")
            self.stream.write(f"{self._active_prompt}{buffer}")
            self.stream.flush()
            if readline is not None:
                try:
                    readline.redisplay()
                except Exception:
                    pass
            return
        self.stream.write(f"{text}\n")
        self.stream.flush()

    @staticmethod
    def _current_input_buffer() -> str:
        if readline is None:
            return ""
        try:
            return readline.get_line_buffer()
        except Exception:
            return ""


def _parse_reply_command(raw_text: str) -> tuple[Optional[str], Optional[str]]:
    return parse_reply_command(raw_text)


async def _handle_local_session_command(
    *,
    raw_text: str,
    application: GatewayApplication,
    current_session_key: Optional[str],
    channel_name: Optional[str] = None,
    user_id: Optional[str] = None,
    chat_id: Optional[str] = None,
    thread_id: Optional[str] = None,
    query_handler: Optional[object] = None,
) -> tuple[bool, Optional[str], Optional[str]]:
    result = await application.execute_interactive_command(
        raw_text=raw_text,
        current_session_key=current_session_key,
        channel_name=channel_name or "cli",
        user_id=user_id or "cli",
        chat_id=chat_id or "cli",
        thread_id=thread_id,
        help_text=INTERACTIVE_SESSION_HELP,
        query_handler=query_handler,
    )
    return result.handled, result.session_key, result.text


def _add_openai_codex_path_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--auth-store-path",
        default=None,
        help="Command-local OpenAI Codex auth store path override; does not edit instance_config.yaml",
    )
    parser.add_argument(
        "--host-data-root",
        default=None,
        help="Host directory bind-mounted into the container, used to map container auth paths for Docker imports",
    )
    parser.add_argument(
        "--container-data-root",
        default="/data",
        help="Container mount root paired with --host-data-root. Default: /data",
    )


def _add_xai_oauth_path_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--auth-store-path",
        default=None,
        help="Command-local xAI OAuth auth store path override; does not edit instance_config.yaml",
    )
    parser.add_argument(
        "--host-data-root",
        default=None,
        help="Host directory bind-mounted into the container, used to map a container auth path",
    )
    parser.add_argument(
        "--container-data-root",
        default="/data",
        help="Container mount root paired with --host-data-root. Default: /data",
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Assistant gateway CLI for Phase 1 and Phase 2"
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    onboard = subparsers.add_parser(
        "onboard", help="Create a new gateway instance config and workspace"
    )
    onboard.add_argument(
        "--instance-name", default=None, help="Logical name for the gateway instance"
    )
    onboard.add_argument(
        "--instance-root", default=None, help="Directory for the instance root"
    )
    onboard.add_argument("--team-id", default=None, help="Target team id")
    onboard.add_argument("--timezone", default=None, help="User timezone")
    onboard.add_argument("--display-name", default=None, help="User display name")
    onboard.add_argument(
        "--assistant-name", default=None, help="Assistant display name"
    )
    onboard.add_argument(
        "--assistant-purpose", default="", help="Short assistant purpose statement"
    )
    onboard.add_argument(
        "--assistant-response-style",
        default="",
        help="Preferred assistant response style",
    )
    onboard.add_argument(
        "--assistant-autonomy", default="", help="Assistant autonomy preferences"
    )
    onboard.add_argument("--user-role", default="", help="User role or title")
    onboard.add_argument(
        "--user-context", default="", help="Initial context about the user"
    )
    onboard.add_argument(
        "--default-delivery-channel",
        dest="default_delivery_channel",
        default=None,
        help="Default delivery channel label",
    )
    onboard.add_argument(
        "--default-delivery-chat-id",
        dest="default_delivery_chat_id",
        default=None,
        help="Default delivery chat identifier",
    )
    onboard.add_argument(
        "--default-delivery-thread-id",
        dest="default_delivery_thread_id",
        default=None,
        help="Default delivery channel thread/topic identifier",
    )
    onboard.add_argument(
        "--preferred-channel",
        dest="default_delivery_channel",
        default=None,
        help=argparse.SUPPRESS,
    )
    onboard.add_argument(
        "--preferred-output-chat-id",
        dest="default_delivery_chat_id",
        default=None,
        help=argparse.SUPPRESS,
    )
    onboard.add_argument(
        "--preferred-channel-thread-id",
        dest="default_delivery_thread_id",
        default=None,
        help=argparse.SUPPRESS,
    )
    onboard.add_argument(
        "--response-queue-name", default=None, help="Dedicated user response queue"
    )
    onboard.add_argument(
        "--manager-agent-id", default=None, help="Pinned manager agent id"
    )
    onboard.add_argument(
        "--request-timeout-seconds",
        type=int,
        default=None,
        help="Gateway wait timeout for one interactive manager response",
    )
    onboard.add_argument(
        "--quiet-start-hour",
        type=int,
        default=None,
        help="Quiet hours start in local time",
    )
    onboard.add_argument(
        "--quiet-end-hour", type=int, default=None, help="Quiet hours end in local time"
    )
    onboard.add_argument(
        "--disable-quiet-hours",
        action="store_true",
        default=None,
        help="Disable quiet hours",
    )
    onboard.add_argument(
        "--digest-hour",
        type=int,
        default=None,
        help="Deferred delivery digest hour in local time",
    )
    onboard.add_argument(
        "--heartbeat-interval-seconds",
        type=int,
        default=None,
        help="Heartbeat interval",
    )
    onboard.add_argument(
        "--disable-heartbeat",
        action="store_true",
        default=None,
        help="Disable heartbeat checks",
    )
    onboard.add_argument(
        "--heartbeat-active-hours",
        action="store_true",
        default=None,
        help="Enable heartbeat active-hours window",
    )
    onboard.add_argument(
        "--heartbeat-active-start-hour",
        type=int,
        default=None,
        help="Heartbeat active-hours start in local time",
    )
    onboard.add_argument(
        "--heartbeat-active-end-hour",
        type=int,
        default=None,
        help="Heartbeat active-hours end in local time",
    )
    onboard.add_argument(
        "--model-provider", default=None, help="Preferred model provider label"
    )
    onboard.add_argument("--model-id", default=None, help="Preferred model id")
    onboard.add_argument(
        "--reasoning-effort", default=None, help="Preferred reasoning effort"
    )
    onboard.add_argument(
        "--reasoning-summary",
        choices=sorted(REASONING_SUMMARY_VALUES),
        default=None,
        help="Provider-generated Responses reasoning summary",
    )
    onboard.add_argument(
        "--litellm-api-mode",
        choices=sorted(LITELLM_API_MODES),
        default=None,
        help="LiteLLM request API",
    )
    onboard.add_argument(
        "--non-interactive",
        action="store_true",
        help="Do not prompt; require explicit arguments",
    )
    onboard.add_argument(
        "--base-config",
        default=None,
        help="Optional base config.yaml path for runtime/team defaults",
    )
    onboard.add_argument(
        "--base-env-file",
        default=None,
        help="Optional source .env file to copy into the instance root for runtime launch",
    )
    onboard.add_argument(
        "--env",
        default=None,
        help="Optional .env file to load before resolving onboarding defaults",
    )
    internal_credentials_group = onboard.add_mutually_exclusive_group()
    internal_credentials_group.add_argument(
        "--generate-internal-credentials",
        dest="generate_internal_credentials",
        action="store_true",
        default=None,
        help="Ensure missing message-broker/Postgres/activity/secure-tool credentials exist in team.env (default)",
    )
    internal_credentials_group.add_argument(
        "--no-generate-internal-credentials",
        dest="generate_internal_credentials",
        action="store_false",
        default=None,
        help="Do not create or update internal credentials in team.env during onboarding",
    )
    onboard.add_argument(
        "--rotate-internal-credentials",
        action="store_true",
        default=False,
        help="Rotate generated message-broker/Postgres/activity/secure-tool credentials in team.env",
    )
    onboard.add_argument(
        "--secrets-from-environment",
        action="store_true",
        default=False,
        help=(
            "Secrets come only from environment variables and Docker secrets (*_FILE): write no "
            "team.env, generate nothing, and list any required secret the environment lacks. "
            "Stored as launch.secrets_source: environment; later runs keep it."
        ),
    )
    onboard.add_argument(
        "--security-layer-mode",
        choices=list(SECURITY_LAYER_MODES),
        default=None,
        help=(
            "Security mode: broker, or openshell to also run agent commands in an OpenShell "
            "sandbox. The secure tool broker always runs."
        ),
    )
    onboard.add_argument(
        "--public-origin",
        default=None,
        help="Browser-facing scheme and host shared by public service URLs",
    )
    onboard.add_argument(
        "--secure-tool-broker-internal-base-url",
        default=None,
        help="Agent/gateway-facing URL for secure tool broker API calls",
    )
    onboard.add_argument(
        "--secure-tool-broker-public-port",
        type=int,
        default=None,
        help="Browser-facing secure tool broker port combined with --public-origin",
    )
    onboard.add_argument(
        "--secure-tool-broker-bind-host",
        default=None,
        help="Bind host for the secure tool broker",
    )
    onboard.add_argument(
        "--secure-tool-broker-bind-port",
        type=int,
        default=None,
        help="Bind port for the secure tool broker",
    )
    onboard.add_argument(
        "--secure-tool-broker-credentials-dir",
        default=None,
        help="Instance-root-relative broker-only credential state directory",
    )
    onboard.add_argument(
        "--internal-website-bind-host",
        default=None,
        help="Bind host for the internal website app",
    )
    onboard.add_argument(
        "--internal-website-bind-port",
        type=int,
        default=None,
        help="Bind port for the internal website app",
    )
    onboard.add_argument(
        "--internal-website-public-port",
        type=int,
        default=None,
        help="Browser-facing internal website port combined with --public-origin",
    )
    onboard.add_argument(
        "--internal-website-docs-root",
        default=None,
        help="Static docs root for the internal website app",
    )
    onboard.add_argument(
        "--internal-website-skills-dir",
        default=None,
        help="Skills docs source directory for the internal website app",
    )
    onboard.add_argument(
        "--enable-discord",
        action="store_true",
        default=None,
        help="Enable the Discord interactive app",
    )
    onboard.add_argument(
        "--discord-allow-user-id",
        action="append",
        default=None,
        help="Allowed Discord user id; repeatable",
    )
    onboard.add_argument(
        "--discord-allow-channel-id",
        action="append",
        default=None,
        help="Allowed Discord channel id; repeatable",
    )
    onboard.add_argument(
        "--discord-group-policy",
        default=None,
        help="Discord group policy: open or mention",
    )
    onboard.add_argument(
        "--discord-thread-scoped-sessions",
        action="store_true",
        default=None,
        help="Scope Discord sessions by thread",
    )
    onboard.add_argument(
        "--discord-command-guild-id",
        action="append",
        default=None,
        help="Guild id to sync Discord slash commands to immediately; repeatable",
    )
    onboard.add_argument(
        "--enable-telegram",
        action="store_true",
        default=None,
        help="Enable the Telegram interactive app",
    )
    onboard.add_argument(
        "--telegram-allow-user-id",
        action="append",
        default=None,
        help="Allowed Telegram user id; repeatable",
    )
    onboard.add_argument(
        "--telegram-allow-chat-id",
        action="append",
        default=None,
        help="Allowed Telegram chat id; repeatable",
    )
    onboard.add_argument(
        "--telegram-group-policy",
        default=None,
        help="Telegram group policy: open or mention",
    )
    onboard.add_argument(
        "--telegram-manager-offline-policy",
        default=None,
        choices=[
            TELEGRAM_MANAGER_OFFLINE_POLICY_NOTIFY,
            TELEGRAM_MANAGER_OFFLINE_POLICY_PAUSE_POLLING,
        ],
        help="Telegram manager-offline policy: notify or pause_telegram_polling",
    )
    onboard.add_argument(
        "--telegram-topic-scoped-sessions",
        action="store_true",
        default=None,
        help="Scope Telegram sessions by forum topic/thread",
    )
    onboard.add_argument(
        "--telegram-drop-pending-updates",
        action="store_true",
        default=None,
        help="Drop pending Telegram updates on startup",
    )
    onboard.add_argument(
        "--enable-signal",
        action="store_true",
        default=None,
        help="Enable the Signal interactive app",
    )
    onboard.add_argument(
        "--signal-base-url",
        default=None,
        help="Base URL for the signal-cli-rest-api server",
    )
    onboard.add_argument(
        "--signal-account",
        default=None,
        help="Registered Signal account or number used by signal-cli-rest-api",
    )
    onboard.add_argument(
        "--signal-allow-sender-id",
        action="append",
        default=None,
        help="Allowed Signal direct sender id, phone number, or UUID; repeatable",
    )
    onboard.add_argument(
        "--signal-allow-group-id",
        action="append",
        default=None,
        help="Allowed Signal group id; repeatable",
    )
    onboard.add_argument(
        "--signal-group-policy",
        choices=["open", "mention"],
        default=None,
        help="Signal group policy: open or mention",
    )
    onboard.add_argument(
        "--signal-mention-marker",
        action="append",
        default=None,
        help="Mention marker required in Signal groups; repeatable",
    )
    onboard.add_argument(
        "--signal-poll-timeout-seconds",
        type=float,
        default=None,
        help="Signal receive long-poll timeout in seconds",
    )
    onboard.add_argument(
        "--signal-max-messages-per-poll",
        type=int,
        default=None,
        help="Maximum Signal messages requested per receive poll",
    )
    onboard.add_argument(
        "--signal-drop-pending-messages-on-startup",
        action="store_true",
        default=None,
        help="Drop queued Signal receive messages when the adapter starts",
    )
    onboard.add_argument(
        "--enable-photon",
        action="store_true",
        default=None,
        help="Enable the Photon Spectrum iMessage interactive app",
    )
    onboard.add_argument(
        "--photon-project-id",
        default=None,
        help="Photon Spectrum PHOTON_PROJECT_ID value",
    )
    onboard.add_argument(
        "--photon-project-secret",
        default=None,
        help="Photon Spectrum PHOTON_PROJECT_SECRET value written to team.env",
    )
    onboard.add_argument(
        "--photon-allow-sender-id",
        action="append",
        default=None,
        help="Allowed Photon direct sender id; repeatable",
    )
    onboard.add_argument(
        "--photon-allow-group-id",
        action="append",
        default=None,
        help="Allowed Photon group id; repeatable",
    )
    onboard.add_argument(
        "--photon-group-policy",
        choices=["open", "mention"],
        default=None,
        help="Photon group policy: open or mention",
    )
    onboard.add_argument(
        "--photon-mention-marker",
        action="append",
        default=None,
        help="Mention marker required in Photon groups; repeatable",
    )
    onboard.add_argument(
        "--enable-whatsapp",
        action="store_true",
        default=None,
        help="Enable the WhatsApp interactive app",
    )
    onboard.add_argument(
        "--whatsapp-allow-sender-id",
        action="append",
        default=None,
        help="Allowed WhatsApp sender id or phone number; repeatable",
    )
    onboard.add_argument(
        "--whatsapp-webhook-host",
        default=None,
        help="Host/interface for the WhatsApp webhook listener",
    )
    onboard.add_argument(
        "--whatsapp-webhook-port",
        type=int,
        default=None,
        help="Port for the WhatsApp webhook listener",
    )
    onboard.add_argument(
        "--whatsapp-webhook-path",
        default=None,
        help="Path for the WhatsApp webhook listener",
    )
    onboard.add_argument(
        "--whatsapp-public-base-url",
        default=None,
        help="Public HTTPS base URL used by Twilio to reach the webhook",
    )
    onboard.add_argument(
        "--whatsapp-disable-signature-validation",
        action="store_false",
        default=None,
        dest="whatsapp_validate_twilio_signature",
        help="Disable Twilio signature validation for the WhatsApp webhook",
    )

    onboard_web = subparsers.add_parser(
        "onboard-web", help="Run browser-based onboarding and write a real instance"
    )
    onboard_web.add_argument(
        "--host",
        default=os.getenv("ONBOARD_WEB_BIND_HOST", "0.0.0.0"),
        help="Web onboarding bind host",
    )
    onboard_web.add_argument(
        "--port",
        type=int,
        default=int(os.getenv("ONBOARD_WEB_BIND_PORT", "18090")),
        help="Web onboarding bind port",
    )
    onboard_web.add_argument(
        "--instance-root",
        default=os.getenv("TEAM_INSTANCE_ROOT", "/data"),
        help="Directory for the instance root",
    )
    onboard_web.add_argument(
        "--base-config",
        default=None,
        help="Optional base config.yaml path for runtime/team defaults",
    )
    onboard_web.add_argument(
        "--base-env-file",
        default=None,
        help="Optional source .env file to merge into team.env",
    )
    onboard_web.add_argument(
        "--env",
        default=None,
        help="Optional .env file to load before resolving onboarding defaults",
    )
    onboard_web.add_argument(
        "--no-restart-on-complete",
        dest="restart_on_complete",
        action="store_false",
        default=True,
        help="Keep the onboarding web server running after completion instead of exiting for container restart",
    )

    onbard = subparsers.add_parser("onbard", help=argparse.SUPPRESS)
    for action in onboard._actions[1:]:
        onbard._add_action(action)

    chat = subparsers.add_parser(
        "chat", help="Start an interactive local chat with the manager"
    )
    chat.add_argument(
        "--config", required=False, default=None, help="Path to instance_config.yaml"
    )
    chat.add_argument("--session-key", default=None, help="Override session key")
    chat.add_argument(
        "--env", default=None, help="Optional .env file to load before connecting"
    )

    bridge_chat = subparsers.add_parser(
        "bridge-chat",
        help="Start an interactive chat through the Phase 2 translation bridge",
    )
    bridge_chat.add_argument(
        "--config", required=False, default=None, help="Path to instance_config.yaml"
    )
    bridge_chat.add_argument("--session-key", default=None, help="Override session key")
    bridge_chat.add_argument(
        "--channel-name", default="local", help="Logical channel name"
    )
    bridge_chat.add_argument("--chat-id", default="local-chat", help="Logical chat id")
    bridge_chat.add_argument("--user-id", default="local-user", help="Logical user id")
    bridge_chat.add_argument(
        "--env", default=None, help="Optional .env file to load before connecting"
    )

    run_gateway = subparsers.add_parser(
        "run-gateway", help="Run gateway background services without chat"
    )
    run_gateway.add_argument(
        "--config", required=False, default=None, help="Path to instance_config.yaml"
    )

    run_adapter_host = subparsers.add_parser(
        "run-adapter-host",
        help="Run configured out-of-process interactive app plugins",
    )
    run_adapter_host.add_argument(
        "--config", required=False, default=None, help="Path to instance_config.yaml"
    )
    run_adapter_host.add_argument(
        "--env", default=None, help="Optional .env file to load before connecting"
    )
    run_gateway.add_argument(
        "--env", default=None, help="Optional .env file to load before connecting"
    )
    run_gateway.add_argument(
        "--no-apps",
        action="store_true",
        help="Do not start configured interactive apps",
    )

    run_activity = subparsers.add_parser(
        "run-activity-collector",
        help="Run the standalone activity collector and dashboard",
    )
    run_activity.add_argument(
        "--config", required=False, default=None, help="Path to instance_config.yaml"
    )
    run_activity.add_argument(
        "--env", default=None, help="Optional .env file to load before connecting"
    )

    run_broker = subparsers.add_parser(
        "run-secure-tool-broker", help="Run the credential-owning secure CLI broker"
    )
    run_broker.add_argument(
        "--config", required=False, default=None, help="Path to instance_config.yaml"
    )
    run_broker.add_argument(
        "--env", default=None, help="Optional .env file to load before starting"
    )
    run_broker.add_argument(
        "--broker-env-file",
        default=None,
        help="Optional broker-only env file for credentialed CLI credentials",
    )
    run_broker.add_argument(
        "--host", default=None, help="Bind host; defaults from config/env or 0.0.0.0"
    )
    run_broker.add_argument(
        "--port",
        type=int,
        default=None,
        help="Bind port; defaults from config/env or 18600",
    )

    run_app = subparsers.add_parser(
        "run-app", help="Run one configured interactive app against the live gateway"
    )
    run_app.add_argument(
        "--config", required=False, default=None, help="Path to instance_config.yaml"
    )
    run_app.add_argument(
        "--app",
        required=True,
        help="Registered interactive-app plugin id or channel name",
    )
    run_app.add_argument(
        "--env", default=None, help="Optional .env file to load before connecting"
    )

    list_apps = subparsers.add_parser(
        "interactive-app-list",
        help="List configured adapters, capabilities, package provenance, and diagnostics",
    )
    list_apps.add_argument(
        "--config", required=False, default=None, help="Path to instance_config.yaml"
    )
    list_apps.add_argument(
        "--env", default=None, help="Optional .env file to load before inspection"
    )

    run_execution = subparsers.add_parser(
        "run-execution-controller",
        help="Run immutable schedules, fenced attempts, and managed services",
    )
    run_execution.add_argument(
        "--config", required=False, default=None, help="Path to instance_config.yaml"
    )
    run_execution.add_argument(
        "--env", default=None, help="Optional .env file to load before connecting"
    )

    execution_list = subparsers.add_parser(
        "execution-job-list", help="List team-scoped immutable execution jobs"
    )
    execution_list.add_argument(
        "--config", required=False, default=None, help="Path to instance_config.yaml"
    )
    execution_list.add_argument(
        "--env", default=None, help="Optional .env file to load before reading storage"
    )
    execution_list.add_argument(
        "--include-disabled", action="store_true", help="Include disabled jobs"
    )
    execution_list.add_argument("--json", action="store_true", help="Print JSON")

    execution_show = subparsers.add_parser(
        "execution-job-show", help="Show one team-scoped execution job and recent attempts"
    )
    execution_show.add_argument(
        "--config", required=False, default=None, help="Path to instance_config.yaml"
    )
    execution_show.add_argument(
        "--env", default=None, help="Optional .env file to load before reading storage"
    )
    execution_show.add_argument("--job-id", required=True, help="Execution job id")

    execution_retire = subparsers.add_parser(
        "execution-retire-old-records",
        help=(
            "Disable jobs, stop services and close SSH sessions saved before access model 2, "
            "so the execution controller can start"
        ),
    )
    execution_retire.add_argument(
        "--config", required=False, default=None, help="Path to instance_config.yaml"
    )
    execution_retire.add_argument(
        "--env", default=None, help="Optional .env file to load before reading storage"
    )

    cron_add = subparsers.add_parser(
        "cron-add", help="Create a manager-owned scheduled job"
    )
    cron_add.add_argument(
        "--config", required=False, default=None, help="Path to instance_config.yaml"
    )
    cron_add.add_argument(
        "--env", default=None, help="Optional .env file to load before connecting"
    )
    cron_add.add_argument(
        "--session-key", default="cron", help="Session key for the autonomous lane"
    )
    cron_add.add_argument("--name", required=True, help="Human-friendly job name")
    cron_add.add_argument(
        "--instruction", required=True, help="Instruction sent to the manager"
    )
    cron_add.add_argument(
        "--interval-seconds", type=int, default=None, help="Run every N seconds"
    )
    cron_add.add_argument(
        "--cron-expression", default=None, help="Standard cron expression"
    )
    cron_list = subparsers.add_parser(
        "cron-list", help="List manager-owned scheduled jobs"
    )
    cron_list.add_argument(
        "--config", required=False, default=None, help="Path to instance_config.yaml"
    )
    cron_list.add_argument(
        "--env", default=None, help="Optional .env file to load before connecting"
    )

    cron_remove = subparsers.add_parser(
        "cron-remove", help="Remove a manager-owned scheduled job"
    )
    cron_remove.add_argument(
        "--config", required=False, default=None, help="Path to instance_config.yaml"
    )
    cron_remove.add_argument(
        "--env", default=None, help="Optional .env file to load before connecting"
    )
    cron_remove.add_argument(
        "--job-id", required=True, help="Job id returned by cron-add"
    )

    cron_pause = subparsers.add_parser(
        "cron-pause", help="Temporarily pause a manager-owned scheduled job"
    )
    cron_pause.add_argument(
        "--config", required=False, default=None, help="Path to instance_config.yaml"
    )
    cron_pause.add_argument(
        "--env", default=None, help="Optional .env file to load before connecting"
    )
    cron_pause.add_argument("--job-id", required=True, help="Job id to pause")
    cron_pause.add_argument(
        "--minutes", type=int, default=None, help="Pause duration in minutes"
    )
    cron_pause.add_argument(
        "--until-iso",
        default=None,
        help="UTC ISO timestamp until which the job stays paused",
    )

    cron_resume = subparsers.add_parser(
        "cron-resume", help="Resume a paused or disabled manager-owned scheduled job"
    )
    cron_resume.add_argument(
        "--config", required=False, default=None, help="Path to instance_config.yaml"
    )
    cron_resume.add_argument(
        "--env", default=None, help="Optional .env file to load before connecting"
    )
    cron_resume.add_argument("--job-id", required=True, help="Job id to resume")

    cron_disable = subparsers.add_parser(
        "cron-disable", help="Disable a manager-owned scheduled job until resumed"
    )
    cron_disable.add_argument(
        "--config", required=False, default=None, help="Path to instance_config.yaml"
    )
    cron_disable.add_argument(
        "--env", default=None, help="Optional .env file to load before connecting"
    )
    cron_disable.add_argument("--job-id", required=True, help="Job id to disable")

    objective_list = subparsers.add_parser(
        "objective-list", help="List manager-owned Objectives"
    )
    objective_list.add_argument(
        "--config", required=False, default=None, help="Path to instance_config.yaml"
    )
    objective_list.add_argument(
        "--env", default=None, help="Optional .env file to load before reading storage"
    )
    objective_list.add_argument(
        "--owner-agent-id",
        default=None,
        help="Manager agent id that owns the Objective records",
    )
    objective_list.add_argument(
        "--limit", type=int, default=20, help="Maximum Objective rows to show"
    )
    objective_list.add_argument(
        "--status", default="", help="Optional comma-separated Objective statuses"
    )
    objective_list.add_argument(
        "--include-closed",
        action="store_true",
        help="Include satisfied/failed/cancelled/abandoned Objectives",
    )

    objective_show = subparsers.add_parser(
        "objective-show", help="Show one manager-owned Objective as JSON"
    )
    objective_show.add_argument(
        "--config", required=False, default=None, help="Path to instance_config.yaml"
    )
    objective_show.add_argument(
        "--env", default=None, help="Optional .env file to load before reading storage"
    )
    objective_show.add_argument(
        "--owner-agent-id",
        default=None,
        help="Manager agent id that owns the Objective record",
    )
    objective_show.add_argument(
        "--objective-id", required=True, help="Objective id to show"
    )

    heartbeat_pause = subparsers.add_parser(
        "heartbeat-pause", help="Temporarily pause the manager-owned heartbeat schedule"
    )
    heartbeat_pause.add_argument(
        "--config", required=False, default=None, help="Path to instance_config.yaml"
    )
    heartbeat_pause.add_argument(
        "--env", default=None, help="Optional .env file to load before connecting"
    )
    heartbeat_pause.add_argument(
        "--minutes", type=int, default=None, help="Pause duration in minutes"
    )
    heartbeat_pause.add_argument(
        "--until-iso",
        default=None,
        help="UTC ISO timestamp until which heartbeat stays paused",
    )

    heartbeat_resume = subparsers.add_parser(
        "heartbeat-resume", help="Resume the manager-owned heartbeat schedule"
    )
    heartbeat_resume.add_argument(
        "--config", required=False, default=None, help="Path to instance_config.yaml"
    )
    heartbeat_resume.add_argument(
        "--env", default=None, help="Optional .env file to load before connecting"
    )

    heartbeat_disable = subparsers.add_parser(
        "heartbeat-disable",
        help="Disable the manager-owned heartbeat schedule until resumed",
    )
    heartbeat_disable.add_argument(
        "--config", required=False, default=None, help="Path to instance_config.yaml"
    )
    heartbeat_disable.add_argument(
        "--env", default=None, help="Optional .env file to load before connecting"
    )

    deliver = subparsers.add_parser(
        "deliver-pending", help="Release any due deferred deliveries"
    )
    deliver.add_argument(
        "--config", required=False, default=None, help="Path to instance_config.yaml"
    )
    deliver.add_argument(
        "--env", default=None, help="Optional .env file to load before connecting"
    )

    memory_graph_status = subparsers.add_parser(
        "memory-graph-embedding-status",
        help="Inspect configured vs stored memory-graph embedding profiles",
    )
    memory_graph_status.add_argument(
        "--config", required=False, default=None, help="Path to instance_config.yaml"
    )
    memory_graph_status.add_argument(
        "--env", default=None, help="Optional .env file to load before connecting"
    )

    memory_graph_reset = subparsers.add_parser(
        "memory-graph-embedding-reset",
        help="Reset stored memory-graph embedding state after a provider/model change",
    )
    memory_graph_reset.add_argument(
        "--config", required=False, default=None, help="Path to instance_config.yaml"
    )
    memory_graph_reset.add_argument(
        "--env", default=None, help="Optional .env file to load before connecting"
    )
    reset_group = memory_graph_reset.add_mutually_exclusive_group(required=True)
    reset_group.add_argument(
        "--archive",
        action="store_true",
        help="Archive the active embedding profile and clear live graph embeddings",
    )
    reset_group.add_argument(
        "--purge",
        action="store_true",
        help="Delete all stored graph embeddings and profile history",
    )

    codex_status = subparsers.add_parser(
        "openai-codex-status", help="Inspect OpenAI Codex OAuth credential status"
    )
    codex_status.add_argument(
        "--config", required=False, default=None, help="Path to instance_config.yaml"
    )
    codex_status.add_argument(
        "--env",
        default=None,
        help="Optional .env file to load before resolving credentials",
    )
    _add_openai_codex_path_args(codex_status)
    codex_status.add_argument("--json", action="store_true", help="Emit JSON")

    codex_import = subparsers.add_parser(
        "openai-codex-import", help="Import file-based Codex CLI OAuth tokens"
    )
    codex_import.add_argument(
        "--config", required=False, default=None, help="Path to instance_config.yaml"
    )
    codex_import.add_argument(
        "--env", default=None, help="Optional .env file to load before resolving paths"
    )
    codex_import.add_argument(
        "--codex-home", required=True, help="Codex CLI home containing auth.json"
    )
    _add_openai_codex_path_args(codex_import)

    codex_models = subparsers.add_parser(
        "openai-codex-models", help="List discoverable OpenAI Codex OAuth models"
    )
    codex_models.add_argument(
        "--config", required=False, default=None, help="Path to instance_config.yaml"
    )
    codex_models.add_argument(
        "--env",
        default=None,
        help="Optional .env file to load before resolving credentials",
    )
    _add_openai_codex_path_args(codex_models)
    codex_models.add_argument(
        "--refresh",
        action="store_true",
        help="Try live discovery before cache/default fallbacks",
    )
    codex_models.add_argument("--json", action="store_true", help="Emit JSON")

    xai_login = subparsers.add_parser(
        "xai-oauth-login", help="Sign in to xAI with the OAuth device flow"
    )
    xai_login.add_argument(
        "--config",
        required=False,
        default=None,
        help="Optional path to instance_config.yaml",
    )
    xai_login.add_argument(
        "--instance-root",
        required=False,
        default=None,
        help="Instance root to use before instance_config.yaml exists",
    )
    xai_login.add_argument(
        "--env", default=None, help="Optional .env file to load before resolving paths"
    )
    _add_xai_oauth_path_args(xai_login)
    xai_login.add_argument(
        "--no-browser",
        action="store_true",
        help="Print the verification URL without opening it",
    )
    xai_login.add_argument(
        "--timeout",
        type=float,
        default=900.0,
        help="Maximum seconds to wait for device approval. Default: 900",
    )

    xai_status = subparsers.add_parser(
        "xai-oauth-status", help="Inspect xAI OAuth credential status"
    )
    xai_status.add_argument(
        "--config",
        required=False,
        default=None,
        help="Optional path to instance_config.yaml",
    )
    xai_status.add_argument(
        "--instance-root",
        required=False,
        default=None,
        help="Instance root to inspect before instance_config.yaml exists",
    )
    xai_status.add_argument(
        "--env",
        default=None,
        help="Optional .env file to load before resolving credentials",
    )
    _add_xai_oauth_path_args(xai_status)
    xai_status.add_argument(
        "--refresh",
        action="store_true",
        help="Force a token refresh while checking status",
    )
    xai_status.add_argument("--json", action="store_true", help="Emit JSON")

    mcp_levels = subparsers.add_parser(
        "mcp-operation-levels",
        help=(
            "Show or confirm the access level (read, write, full) of installed MCP "
            "operations; unconfirmed operations need full access"
        ),
    )
    mcp_levels.add_argument("--config", required=True, help="Path to instance_config.yaml")
    mcp_levels.add_argument("--connection", default=None, help="MCP connection name")
    mcp_levels.add_argument(
        "--set",
        action="append",
        default=[],
        metavar="OPERATION=LEVEL",
        help="Confirm one operation's level (repeatable; needs --connection)",
    )
    mcp_levels.add_argument(
        "--accept-suggested",
        action="store_true",
        help="Confirm the suggested level of every unconfirmed operation (needs --connection)",
    )
    mcp_levels.add_argument(
        "--by",
        default=None,
        help="The administrator confirming levels (default: the instance's default user)",
    )

    access = subparsers.add_parser(
        "access",
        help="Give, revoke, block or list agents' and people's access to tools",
    )
    access_commands = access.add_subparsers(dest="access_command", required=True)
    access_list = access_commands.add_parser("list", help="List grants and blocks in the team")
    access_list.add_argument("--config", required=True, help="Path to instance_config.yaml")
    access_list.add_argument("--all", action="store_true", help="Include revoked and refused grants")
    access_list.add_argument(
        "match", nargs="?", default=None, help="Only grants about this agent type, agent, person, tool or tool set"
    )
    access_grant = access_commands.add_parser(
        "grant", help="Give access as a person, within that person's own authority"
    )
    access_grant.add_argument("--config", required=True, help="Path to instance_config.yaml")
    access_grant.add_argument(
        "--by", default=None, help="The person giving access (default: the instance's default user)"
    )
    access_grant.add_argument(
        "--to",
        required=True,
        help="agent-type:<Type>, agent:<id>, role:<name>, user:<id>, or team",
    )
    access_grant.add_argument("--tool", default=None, help="Tool id (access is a level on the whole tool)")
    access_grant.add_argument("--tool-set", default=None, help="Tool set id")
    access_grant.add_argument(
        "--folder", default=None, help="Folder below team/, agents/ or data/<name>/, such as data/reports/2026"
    )
    access_grant.add_argument("--level", required=True, choices=["read", "write", "full"])
    access_grant.add_argument("--for", dest="duration", default=None, help="Expire after 30m, 12h, 7d, ...")
    access_grant.add_argument(
        "--everyone",
        action="store_true",
        help="Administrators: give agents this access for everyone's work, not only the giver's",
    )
    access_grant.add_argument(
        "--delegate-depth", type=int, default=0, choices=[0, 1, 2], help="How many times it may be handed on"
    )
    access_grant.add_argument(
        "--delegate-mode",
        default="automatic",
        choices=["automatic", "ask_owner"],
        help="Whether hand-downs take effect at once or wait for approval",
    )
    access_grant.add_argument("--reason", default="", help="Why, for the record")
    access_revoke = access_commands.add_parser(
        "revoke", help="Revoke a grant and everything handed down from it"
    )
    access_revoke.add_argument("--config", required=True, help="Path to instance_config.yaml")
    access_revoke.add_argument("--by", default=None, help="The person revoking (default: the default user)")
    access_revoke.add_argument("grant_id", help="The grant to revoke")
    access_block = access_commands.add_parser(
        "block",
        help="Stop an agent type, agent, person, role or the team from using a tool set, tool, operation or folder",
    )
    access_block.add_argument("--config", required=True, help="Path to instance_config.yaml")
    access_block.add_argument("--by", default=None, help="The person blocking (default: the default user)")
    access_block.add_argument("--agent-type", default=None, help="Block every agent of this type")
    access_block.add_argument("--agent", default=None, help="Block one agent by its id")
    access_block.add_argument("--role", default=None, help="Block every holder of a role (worker, member, ...)")
    access_block.add_argument(
        "--team", action="store_true", help="Block everyone in the team, for example from the internet tool set"
    )
    access_block.add_argument(
        "--person", default=None, help="Block a person, and every agent working for them (administrators only)"
    )
    access_block.add_argument(
        "--tool", default=None, help="Tool id, tool#operation for one operation, or a tool set id such as internet"
    )
    access_block.add_argument("--operation", default=None, help="One operation, as tool#operation")
    access_block.add_argument("--folder", default=None, help="Folder below team/, agents/ or data/<name>/")
    access_block.add_argument("--reason", required=True, help="Why, for the record and for the people who see it")
    access_block.add_argument(
        "--hours", type=int, default=None, help="Remove the block after this many hours (default: until removed)"
    )
    access_block.add_argument(
        "--replace", dest="replace_block_id", default=None,
        help="Atomically replace a stale operation block for the same subject and tool, keeping its expiry",
    )
    access_unblock = access_commands.add_parser(
        "unblock", help="Remove a block (the person who added it, or an owner or administrator)"
    )
    access_unblock.add_argument("--config", required=True, help="Path to instance_config.yaml")
    access_unblock.add_argument("--by", default=None, help="The person removing it (default: the default user)")
    access_unblock.add_argument("grant_id", help="The block to remove (from 'access list')")
    access_links = access_commands.add_parser(
        "links", help="List account links: which stored credential each tool uses"
    )
    access_links.add_argument("--config", required=True, help="Path to instance_config.yaml")
    access_links.add_argument("--all", action="store_true", help="Include removed links")
    access_profile = access_commands.add_parser(
        "starter-profile", help="Give the team lead a starter profile's access (replaces the previous one)"
    )
    access_profile.add_argument("--config", required=True, help="Path to instance_config.yaml")
    access_profile.add_argument("--by", default=None, help="The person giving it (default: the default user)")
    access_profile.add_argument("profile", choices=["trusted", "careful", "manual"])
    access_requests = access_commands.add_parser(
        "requests", help="List agents' access requests waiting for a decision"
    )
    access_requests.add_argument("--config", required=True, help="Path to instance_config.yaml")
    access_requests.add_argument("--all", action="store_true", help="Include decided and withdrawn requests")
    access_decide = access_commands.add_parser(
        "decide", help="Decide a waiting access request as a person, within that person's authority"
    )
    access_decide.add_argument("--config", required=True, help="Path to instance_config.yaml")
    access_decide.add_argument("--by", default=None, help="The person deciding (default: the default user)")
    access_decide.add_argument("--note", default="", help="A note the agent sees with the decision")
    access_decide.add_argument("request_id", help="The request to decide (from 'access requests')")
    access_decide.add_argument("choice", choices=["approve", "approve_read", "approve_task", "deny"])
    access_reapprove = access_commands.add_parser(
        "approve-existing",
        help="Re-approve MCP records and scheduled-worker settings saved in the old permission format, as yourself",
    )
    access_reapprove.add_argument("--config", required=True, help="Path to instance_config.yaml")
    access_reapprove.add_argument("--by", default=None, help="The administrator approving (default: the default user)")
    access_unlink = access_commands.add_parser(
        "unlink", help="Remove an account link (your own, or any as a team administrator)"
    )
    access_unlink.add_argument("--config", required=True, help="Path to instance_config.yaml")
    access_unlink.add_argument("--by", default=None, help="The person removing it (default: the default user)")
    access_unlink.add_argument("link_id", help="The account link to remove")
    access_folders = access_commands.add_parser(
        "folders",
        help="List, add or remove data folders: host folders outside the workspace that agents can be given",
    )
    access_folders.add_argument("--config", required=True, help="Path to instance_config.yaml")
    access_folders.add_argument("--by", default=None, help="The person acting (default: the default user)")
    access_folders.add_argument("folder_action", choices=["list", "add", "remove"])
    access_folders.add_argument("name", nargs="?", default=None, help="The folder's name; agents name it data/<name>")
    access_folders.add_argument("path", nargs="?", default=None, help="Absolute host path (add only)")
    access_folders.add_argument("--read-only", action="store_true", help="Refuse writes whatever the grants say")
    access_folders.add_argument("--reason", default="", help="Why it is registered, for the record")
    access_drop_old = access_commands.add_parser(
        "drop-old-tables",
        help="List the database tables the removed permission system left behind; --yes drops them",
    )
    access_drop_old.add_argument("--config", required=True, help="Path to instance_config.yaml")
    access_drop_old.add_argument(
        "--yes", action="store_true", help="Drop the listed tables (without it, only list them)"
    )

    from custom_agents.pinned_memory_transfer import add_memory_parsers

    add_memory_parsers(subparsers)

    from team_transfer.cli import add_team_parsers

    add_team_parsers(subparsers)

    migrate_config = subparsers.add_parser(
        "migrate-config",
        help="Inspect or migrate an instance_config.yaml to the latest schema",
    )
    migrate_config.add_argument(
        "--config", required=True, help="Path to instance_config.yaml"
    )
    migrate_config.add_argument(
        "--yes", action="store_true", help="Apply the migration without prompting"
    )

    return parser


def _value_or_default(raw: Any, default: Any) -> Any:
    return default if raw is None else raw


def _print_onboarding_report(report: OnboardingReport) -> None:
    print("Onboarding reconciliation report:")
    print(f"  Mode: {report.mode}")
    print(
        "  Config paths: "
        f"{len(report.config_added_paths)} added, "
        f"{len(report.config_updated_paths)} updated, "
        f"{len(report.config_removed_paths)} removed"
    )
    print(
        "  Env keys: "
        f"{len(report.env_added_keys)} added, "
        f"{len(report.env_updated_keys)} updated, "
        f"{len(report.env_removed_keys)} removed"
    )
    print(f"  Detailed report: {report.detail_path}")


def _apply_base_template_to_existing_config(
    existing: GatewayInstanceConfig,
    desired: GatewayInstanceConfig,
) -> None:
    """Re-running `onboard --base-config` on an existing instance.

    ``desired`` was built with apply_onboarding_template, so these sections
    now hold the base config's values (execution_control and llm.xai_oauth
    included; before the template was shared they held defaults). The
    security layer, chat channels other than the internal website, MCP
    profiles, gateway user settings and tool_policy are not reapplied: they
    keep the instance's current values.
    """

    existing.gateway.team_id = desired.gateway.team_id
    existing.gateway.storage = desired.gateway.storage.model_copy(deep=True)
    existing.state.storage = desired.state.storage.model_copy(deep=True)
    existing.llm = desired.llm.model_copy(deep=True)
    existing.embedding = desired.embedding.model_copy(deep=True)
    existing.agent = desired.agent.model_copy(deep=True)
    existing.workspace = desired.workspace.model_copy(deep=True)
    existing.agent_types = {
        agent_type: settings.model_copy(deep=True)
        for agent_type, settings in desired.agent_types.items()
    }
    existing.delivery = desired.delivery.model_copy(deep=True)
    existing.network = desired.network.model_copy(deep=True)
    existing.activity_collector = desired.activity_collector.model_copy(deep=True)
    existing.voice = desired.voice.model_copy(deep=True)
    existing.interactive_apps.internal_website = (
        desired.interactive_apps.internal_website.model_copy(deep=True)
    )
    existing.ui = desired.ui.model_copy(deep=True)
    existing.execution_control = desired.execution_control.model_copy(deep=True)


def _reconcile_existing_onboarding_config(
    existing: GatewayInstanceConfig,
    desired: GatewayInstanceConfig,
    args: argparse.Namespace,
) -> GatewayInstanceConfig:
    reconciled = existing.model_copy(deep=True)
    if getattr(args, "base_config", None):
        _apply_base_template_to_existing_config(reconciled, desired)

    if getattr(args, "instance_root", None):
        reconciled.instance_root = desired.instance_root
    if getattr(args, "team_id", None):
        reconciled.gateway.team_id = desired.gateway.team_id
    if getattr(args, "timezone", None):
        reconciled.user.timezone = desired.user.timezone
    if getattr(args, "display_name", None):
        reconciled.user.display_name = desired.user.display_name
        reconciled.user.canonical_user_id = desired.user.canonical_user_id
    if getattr(args, "user_role", ""):
        reconciled.user.role = desired.user.role
    if getattr(args, "user_context", None) is not None:
        reconciled.user.initial_context = desired.user.initial_context
    if getattr(args, "assistant_name", None):
        reconciled.assistant.display_name = desired.assistant.display_name
    if getattr(args, "response_queue_name", None):
        reconciled.gateway.response_queue_name = desired.gateway.response_queue_name
    if getattr(args, "manager_agent_id", None):
        reconciled.gateway.manager_agent_id = desired.gateway.manager_agent_id
    if getattr(args, "request_timeout_seconds", None) is not None:
        reconciled.gateway.request_timeout_seconds = (
            desired.gateway.request_timeout_seconds
        )
    if getattr(args, "disable_quiet_hours", None) is not None:
        reconciled.delivery.quiet_hours.enabled = desired.delivery.quiet_hours.enabled
    if getattr(args, "quiet_start_hour", None) is not None:
        reconciled.delivery.quiet_hours.start_hour_local = (
            desired.delivery.quiet_hours.start_hour_local
        )
    if getattr(args, "quiet_end_hour", None) is not None:
        reconciled.delivery.quiet_hours.end_hour_local = (
            desired.delivery.quiet_hours.end_hour_local
        )
    if getattr(args, "digest_hour", None) is not None:
        reconciled.delivery.digest_delivery_hour_local = (
            desired.delivery.digest_delivery_hour_local
        )
    if getattr(args, "disable_heartbeat", None) is not None:
        reconciled.heartbeat.enabled = desired.heartbeat.enabled
    if getattr(args, "heartbeat_interval_seconds", None) is not None:
        reconciled.heartbeat.interval_seconds = desired.heartbeat.interval_seconds
    if getattr(args, "heartbeat_active_hours", None) is not None:
        reconciled.heartbeat.active_hours_enabled = (
            desired.heartbeat.active_hours_enabled
        )
    if getattr(args, "heartbeat_active_start_hour", None) is not None:
        reconciled.heartbeat.active_start_hour_local = (
            desired.heartbeat.active_start_hour_local
        )
    if getattr(args, "heartbeat_active_end_hour", None) is not None:
        reconciled.heartbeat.active_end_hour_local = (
            desired.heartbeat.active_end_hour_local
        )
    if getattr(args, "model_provider", None) is not None:
        reconciled.llm.provider = desired.llm.provider
    if getattr(args, "model_id", None) is not None:
        reconciled.llm.model_id = desired.llm.model_id
    if getattr(args, "reasoning_effort", None) is not None:
        reconciled.llm.reasoning_effort = desired.llm.reasoning_effort
    if getattr(args, "reasoning_summary", None) is not None:
        reconciled.llm.reasoning_summary = desired.llm.reasoning_summary
    if getattr(args, "litellm_api_mode", None) is not None:
        reconciled.llm.litellm_api_mode = desired.llm.litellm_api_mode
    if getattr(args, "security_layer_mode", None) is not None:
        reconciled.security_layer.mode = desired.security_layer.mode
    if getattr(args, "secure_tool_broker_internal_base_url", None) is not None:
        reconciled.security_layer.broker_internal_base_url = (
            desired.security_layer.broker_internal_base_url
        )
    if getattr(args, "public_origin", None) is not None:
        reconciled.network.public_origin = desired.network.public_origin
    if getattr(args, "secure_tool_broker_public_port", None) is not None:
        reconciled.security_layer.broker_public_port = (
            desired.security_layer.broker_public_port
        )
    if getattr(args, "secure_tool_broker_bind_host", None) is not None:
        reconciled.security_layer.broker_bind_host = (
            desired.security_layer.broker_bind_host
        )
    if getattr(args, "secure_tool_broker_bind_port", None) is not None:
        reconciled.security_layer.broker_bind_port = (
            desired.security_layer.broker_bind_port
        )
    if getattr(args, "secure_tool_broker_credentials_dir", None) is not None:
        reconciled.security_layer.broker_credentials_dir = (
            desired.security_layer.broker_credentials_dir
        )
    if getattr(args, "internal_website_bind_host", None) is not None:
        reconciled.interactive_apps.internal_website.bind_host = (
            desired.interactive_apps.internal_website.bind_host
        )
    if getattr(args, "internal_website_bind_port", None) is not None:
        reconciled.interactive_apps.internal_website.bind_port = (
            desired.interactive_apps.internal_website.bind_port
        )
    if getattr(args, "internal_website_public_port", None) is not None:
        reconciled.interactive_apps.internal_website.public_port = (
            desired.interactive_apps.internal_website.public_port
        )
    if getattr(args, "internal_website_docs_root", None) is not None:
        reconciled.interactive_apps.internal_website.docs_root = (
            desired.interactive_apps.internal_website.docs_root
        )
    if getattr(args, "internal_website_skills_dir", None) is not None:
        reconciled.interactive_apps.internal_website.skills_dir = (
            desired.interactive_apps.internal_website.skills_dir
        )
    if getattr(args, "default_delivery_channel", None) is not None:
        reconciled.delivery.default_target.channel_name = (
            desired.delivery.default_target.channel_name
        )
    if getattr(args, "default_delivery_chat_id", None) is not None:
        reconciled.delivery.default_target.chat_id = (
            desired.delivery.default_target.chat_id
        )
    if getattr(args, "default_delivery_thread_id", None) is not None:
        reconciled.delivery.default_target.thread_id = (
            desired.delivery.default_target.thread_id
        )
    if getattr(args, "enable_discord", None) is not None:
        reconciled.interactive_apps.discord.enabled = (
            desired.interactive_apps.discord.enabled
        )
    if getattr(args, "discord_allow_user_id", None) is not None:
        reconciled.interactive_apps.discord.allowed_user_ids = (
            desired.interactive_apps.discord.allowed_user_ids
        )
    if getattr(args, "discord_allow_channel_id", None) is not None:
        reconciled.interactive_apps.discord.allowed_channel_ids = (
            desired.interactive_apps.discord.allowed_channel_ids
        )
    if getattr(args, "discord_group_policy", None) is not None:
        reconciled.interactive_apps.discord.group_policy = (
            desired.interactive_apps.discord.group_policy
        )
    if getattr(args, "discord_thread_scoped_sessions", None) is not None:
        reconciled.interactive_apps.discord.thread_scoped_sessions = (
            desired.interactive_apps.discord.thread_scoped_sessions
        )
    if getattr(args, "discord_command_guild_id", None) is not None:
        reconciled.interactive_apps.discord.command_guild_ids = (
            desired.interactive_apps.discord.command_guild_ids
        )
    if getattr(args, "enable_telegram", None) is not None:
        reconciled.interactive_apps.telegram.enabled = (
            desired.interactive_apps.telegram.enabled
        )
    if getattr(args, "telegram_allow_user_id", None) is not None:
        reconciled.interactive_apps.telegram.allowed_user_ids = (
            desired.interactive_apps.telegram.allowed_user_ids
        )
    if getattr(args, "telegram_allow_chat_id", None) is not None:
        reconciled.interactive_apps.telegram.allowed_chat_ids = (
            desired.interactive_apps.telegram.allowed_chat_ids
        )
    if getattr(args, "telegram_group_policy", None) is not None:
        reconciled.interactive_apps.telegram.group_policy = (
            desired.interactive_apps.telegram.group_policy
        )
    if getattr(args, "telegram_manager_offline_policy", None) is not None:
        reconciled.interactive_apps.telegram.manager_offline_policy = (
            desired.interactive_apps.telegram.manager_offline_policy
        )
    if getattr(args, "telegram_topic_scoped_sessions", None) is not None:
        reconciled.interactive_apps.telegram.topic_scoped_sessions = (
            desired.interactive_apps.telegram.topic_scoped_sessions
        )
    if getattr(args, "telegram_drop_pending_updates", None) is not None:
        reconciled.interactive_apps.telegram.drop_pending_updates = (
            desired.interactive_apps.telegram.drop_pending_updates
        )
    if getattr(args, "enable_signal", None) is not None:
        reconciled.interactive_apps.signal.enabled = (
            desired.interactive_apps.signal.enabled
        )
    if getattr(args, "signal_base_url", None) is not None:
        reconciled.interactive_apps.signal.base_url = (
            desired.interactive_apps.signal.base_url
        )
    if getattr(args, "signal_account", None) is not None:
        reconciled.interactive_apps.signal.account = (
            desired.interactive_apps.signal.account
        )
    if getattr(args, "signal_allow_sender_id", None) is not None:
        reconciled.interactive_apps.signal.allowed_sender_ids = (
            desired.interactive_apps.signal.allowed_sender_ids
        )
    if getattr(args, "signal_allow_group_id", None) is not None:
        reconciled.interactive_apps.signal.allowed_group_ids = (
            desired.interactive_apps.signal.allowed_group_ids
        )
    if getattr(args, "signal_group_policy", None) is not None:
        reconciled.interactive_apps.signal.group_policy = (
            desired.interactive_apps.signal.group_policy
        )
    if getattr(args, "signal_mention_marker", None) is not None:
        reconciled.interactive_apps.signal.mention_markers = (
            desired.interactive_apps.signal.mention_markers
        )
    if getattr(args, "signal_poll_timeout_seconds", None) is not None:
        reconciled.interactive_apps.signal.poll_timeout_seconds = (
            desired.interactive_apps.signal.poll_timeout_seconds
        )
    if getattr(args, "signal_max_messages_per_poll", None) is not None:
        reconciled.interactive_apps.signal.max_messages_per_poll = (
            desired.interactive_apps.signal.max_messages_per_poll
        )
    if getattr(args, "signal_drop_pending_messages_on_startup", None) is not None:
        reconciled.interactive_apps.signal.drop_pending_messages_on_startup = (
            desired.interactive_apps.signal.drop_pending_messages_on_startup
        )
    if getattr(args, "enable_photon", None) is not None:
        reconciled.interactive_apps.photon.enabled = (
            desired.interactive_apps.photon.enabled
        )
    if getattr(args, "photon_project_id", None) is not None:
        reconciled.interactive_apps.photon.project_id = (
            desired.interactive_apps.photon.project_id
        )
    if getattr(args, "photon_allow_sender_id", None) is not None:
        reconciled.interactive_apps.photon.allowed_sender_ids = (
            desired.interactive_apps.photon.allowed_sender_ids
        )
    if getattr(args, "photon_allow_group_id", None) is not None:
        reconciled.interactive_apps.photon.allowed_group_ids = (
            desired.interactive_apps.photon.allowed_group_ids
        )
    if getattr(args, "photon_group_policy", None) is not None:
        reconciled.interactive_apps.photon.group_policy = (
            desired.interactive_apps.photon.group_policy
        )
    if getattr(args, "photon_mention_marker", None) is not None:
        reconciled.interactive_apps.photon.mention_markers = (
            desired.interactive_apps.photon.mention_markers
        )
    if getattr(args, "enable_whatsapp", None) is not None:
        reconciled.interactive_apps.whatsapp.enabled = (
            desired.interactive_apps.whatsapp.enabled
        )
    if getattr(args, "whatsapp_allow_sender_id", None) is not None:
        reconciled.interactive_apps.whatsapp.allowed_sender_ids = (
            desired.interactive_apps.whatsapp.allowed_sender_ids
        )
    if getattr(args, "whatsapp_webhook_host", None) is not None:
        reconciled.interactive_apps.whatsapp.webhook_host = (
            desired.interactive_apps.whatsapp.webhook_host
        )
    if getattr(args, "whatsapp_webhook_port", None) is not None:
        reconciled.interactive_apps.whatsapp.webhook_port = (
            desired.interactive_apps.whatsapp.webhook_port
        )
    if getattr(args, "whatsapp_webhook_path", None) is not None:
        reconciled.interactive_apps.whatsapp.webhook_path = (
            desired.interactive_apps.whatsapp.webhook_path
        )
    if getattr(args, "whatsapp_public_base_url", None) is not None:
        reconciled.interactive_apps.whatsapp.public_base_url = (
            desired.interactive_apps.whatsapp.public_base_url
        )
    if getattr(args, "whatsapp_validate_twilio_signature", None) is not None:
        reconciled.interactive_apps.whatsapp.validate_twilio_signature = (
            desired.interactive_apps.whatsapp.validate_twilio_signature
        )

    _reconcile_launch_settings(reconciled, desired, args)
    return GatewayInstanceConfig.model_validate(reconciled.model_dump(mode="json"))


def _reconcile_launch_settings(
    reconciled: GatewayInstanceConfig,
    desired: GatewayInstanceConfig,
    args: argparse.Namespace,
) -> None:
    """A rerun keeps the instance's secrets source unless --secrets-from-environment switches it."""

    if getattr(args, "secrets_from_environment", False):
        reconciled.launch.secrets_source = desired.launch.secrets_source
        reconciled.launch.env_file_path = None
    elif not reconciled.launch.secrets_from_environment and not reconciled.launch.env_file_path:
        reconciled.launch.env_file_path = desired.launch.env_file_path


def _should_generate_internal_credentials(args: argparse.Namespace) -> bool:
    if bool(getattr(args, "rotate_internal_credentials", False)):
        if getattr(args, "generate_internal_credentials", None) is False:
            raise SystemExit(
                "--rotate-internal-credentials cannot be combined with --no-generate-internal-credentials"
            )
        return True
    return bool(
        _value_or_default(getattr(args, "generate_internal_credentials", None), True)
    )


def _load_cli_env_file(env_path: str | None) -> None:
    if env_path:
        if load_dotenv is None:
            raise RuntimeError("python-dotenv is required to use --env.")
        load_dotenv(env_path)
        logger.info("Loaded environment variables from %s", env_path)
    materialize_secret_file_envs()


def _load_gateway_instance_config(
    config_path: str, *, env_path: str | None = None
) -> GatewayInstanceConfig:
    _load_cli_env_file(env_path)
    # The saved file plus this deployment's environment (NATS, public origin, website).
    config = GatewayInstanceConfig.load_for_process(config_path)
    logger.info(
        "Loaded gateway instance config from %s with voice enabled=%s input_enabled=%s output_enabled=%s provider=%s.",
        config.config_path,
        config.voice.enabled,
        config.voice.input_enabled,
        config.voice.output_enabled,
        config.voice.provider,
    )
    return config


def _openai_codex_config_payload(config: GatewayInstanceConfig) -> dict[str, Any]:
    return config.llm.openai_codex.model_dump(mode="json")


def _openai_codex_path_context(config: GatewayInstanceConfig) -> str:
    return str(config.instance_root_path)


def _map_container_path_to_host(
    path: str,
    *,
    host_data_root: str,
    container_data_root: str,
) -> str:
    container_root_text = str(container_data_root or "/data").strip() or "/data"
    if not container_root_text.startswith("/"):
        raise ValueError("--container-data-root must be an absolute container path.")
    container_path_text = str(path or "").strip()
    if not container_path_text.startswith("/"):
        raise ValueError(f"Cannot map non-absolute container path: {path!r}")

    container_root = PurePosixPath(container_root_text)
    container_path = PurePosixPath(container_path_text)
    try:
        relative_path = container_path.relative_to(container_root)
    except ValueError as exc:
        raise ValueError(
            f"OAuth auth store path {container_path} is not under "
            f"container data root {container_root}; pass --auth-store-path or adjust "
            "--container-data-root."
        ) from exc
    host_root = Path(host_data_root).expanduser().resolve()
    host_path = (host_root / Path(*relative_path.parts)).resolve()
    try:
        host_path.relative_to(host_root)
    except ValueError as exc:
        raise ValueError(
            f"OAuth auth store path {container_path} escapes host data root {host_root}."
        ) from exc
    return str(host_path)


def _resolve_openai_codex_command_settings(
    settings: Any, args: argparse.Namespace
) -> tuple[Any, dict[str, str]]:
    metadata: dict[str, str] = {}
    auth_store_path = settings.auth_store_path
    if getattr(args, "auth_store_path", None):
        auth_store_path = str(Path(args.auth_store_path).expanduser().resolve())
        metadata["auth_store_path_source"] = "command_override"
    elif getattr(args, "host_data_root", None):
        metadata["container_auth_store_path"] = str(settings.auth_store_path)
        metadata["container_data_root"] = str(
            getattr(args, "container_data_root", None) or "/data"
        )
        metadata["host_data_root"] = str(
            Path(args.host_data_root).expanduser().resolve()
        )
        auth_store_path = _map_container_path_to_host(
            str(settings.auth_store_path),
            host_data_root=args.host_data_root,
            container_data_root=getattr(args, "container_data_root", None) or "/data",
        )
        metadata["auth_store_path_source"] = "host_data_root_translation"
    if auth_store_path == settings.auth_store_path:
        return settings, metadata
    metadata["auth_store_path"] = str(auth_store_path)
    return (
        settings.__class__(
            base_url=settings.base_url,
            auth_store_path=str(auth_store_path),
            shared_codex_home=settings.shared_codex_home,
            allow_shared_codex_auth_read=settings.allow_shared_codex_auth_read,
            refresh_skew_seconds=settings.refresh_skew_seconds,
            refresh_timeout_seconds=settings.refresh_timeout_seconds,
        ),
        metadata,
    )


def _openai_codex_status(args: argparse.Namespace) -> None:
    from custom_agents.openai_codex_auth import (
        OpenAICodexAuthError,
        resolve_openai_codex_credentials,
        resolve_openai_codex_settings,
    )

    config = _load_gateway_instance_config(
        _ensure_instance_config_ready(args.config), env_path=args.env
    )
    instance_root = _openai_codex_path_context(config)
    raw_settings = _openai_codex_config_payload(config)
    settings = resolve_openai_codex_settings(
        raw_settings,
        instance_root=instance_root,
    )
    try:
        settings, path_metadata = _resolve_openai_codex_command_settings(settings, args)
    except ValueError as exc:
        raise SystemExit(str(exc)) from exc
    payload: dict[str, Any] = {
        "ok": False,
        "provider": "openai-codex",
        "base_url": settings.base_url,
        "auth_store_path": settings.auth_store_path,
        "shared_codex_home": settings.shared_codex_home,
        "allow_shared_codex_auth_read": settings.allow_shared_codex_auth_read,
    }
    payload.update(path_metadata)
    try:
        credentials = resolve_openai_codex_credentials(
            settings,
            instance_root=instance_root,
        )
        payload.update(
            {
                "ok": True,
                "source": credentials.source,
                "auth_mode": credentials.auth_mode,
                "last_refresh": credentials.last_refresh,
            }
        )
    except OpenAICodexAuthError as exc:
        payload.update(
            {
                "error": str(exc),
                "code": exc.code,
                "relogin_required": exc.relogin_required,
            }
        )
    if args.json:
        print(json.dumps(payload, indent=2, sort_keys=True))
        return
    if payload["ok"]:
        print(f"OpenAI Codex OAuth credentials are available from {payload['source']}.")
        print(f"Auth store: {payload['auth_store_path']}")
        if payload.get("container_auth_store_path"):
            print(f"Container auth store: {payload['container_auth_store_path']}")
    else:
        print(f"OpenAI Codex OAuth credentials are unavailable: {payload['error']}")


def _openai_codex_import(args: argparse.Namespace) -> None:
    from custom_agents.openai_codex_auth import (
        import_shared_codex_auth,
        resolve_openai_codex_settings,
    )

    config = _load_gateway_instance_config(
        _ensure_instance_config_ready(args.config), env_path=args.env
    )
    instance_root = _openai_codex_path_context(config)

    settings = resolve_openai_codex_settings(
        _openai_codex_config_payload(config),
        instance_root=instance_root,
    )
    try:
        settings, path_metadata = _resolve_openai_codex_command_settings(settings, args)
    except ValueError as exc:
        raise SystemExit(str(exc)) from exc
    path = import_shared_codex_auth(
        codex_home=args.codex_home,
        raw_settings=settings,
        target_auth_store_path=settings.auth_store_path,
    )
    print(f"Imported OpenAI Codex OAuth credentials to {path}")
    if path_metadata.get("container_auth_store_path"):
        print(
            f"Container auth store path: {path_metadata['container_auth_store_path']}"
        )


def _openai_codex_models(args: argparse.Namespace) -> None:
    from custom_agents.openai_codex_auth import (
        OpenAICodexAuthError,
        resolve_openai_codex_credentials,
        resolve_openai_codex_settings,
    )
    from custom_agents.openai_codex_models import (
        get_codex_model_ids,
        resolve_openai_codex_model_discovery_settings,
    )

    config = _load_gateway_instance_config(
        _ensure_instance_config_ready(args.config), env_path=args.env
    )
    instance_root = _openai_codex_path_context(config)
    raw_settings = _openai_codex_config_payload(config)
    auth_settings = resolve_openai_codex_settings(
        raw_settings,
        instance_root=instance_root,
    )
    try:
        auth_settings, _path_metadata = _resolve_openai_codex_command_settings(
            auth_settings, args
        )
    except ValueError as exc:
        raise SystemExit(str(exc)) from exc
    access_token = None
    source = None
    try:
        credentials = resolve_openai_codex_credentials(
            auth_settings,
            instance_root=instance_root,
        )
        access_token = credentials.api_key
        source = credentials.source
    except OpenAICodexAuthError as exc:
        logger.warning(
            "OpenAI Codex credentials unavailable; listing cached/default models: %s",
            exc,
        )
    discovery_settings = resolve_openai_codex_model_discovery_settings(
        raw_settings,
        auth_settings=auth_settings,
        instance_root=instance_root,
    )
    models = get_codex_model_ids(
        discovery_settings,
        access_token=access_token,
        force_refresh=bool(args.refresh),
    )
    if args.json:
        print(
            json.dumps(
                {"models": models, "source": source or "cache_or_defaults"},
                indent=2,
                sort_keys=True,
            )
        )
        return
    for model in models:
        print(model)


def _resolve_xai_oauth_command_settings(
    args: argparse.Namespace,
) -> tuple[Any, str, dict[str, str]]:
    from custom_agents.xai_oauth_auth import resolve_xai_oauth_settings

    _load_cli_env_file(getattr(args, "env", None))
    explicit_config = str(getattr(args, "config", None) or "").strip()
    explicit_root = str(getattr(args, "instance_root", None) or "").strip()
    if explicit_config and explicit_root:
        raise SystemExit("Pass either --config or --instance-root, not both.")

    discovered_config = explicit_config or (
        None if explicit_root else _discover_instance_config_path()
    )
    raw_settings: dict[str, Any]
    if discovered_config:
        config = _load_gateway_instance_config(
            _ensure_instance_config_ready(discovered_config),
            env_path=None,
        )
        instance_root = str(config.instance_root_path)
        raw_settings = config.llm.xai_oauth.model_dump(mode="json")
    else:
        instance_root = (
            explicit_root or str(os.getenv("TEAM_INSTANCE_ROOT") or "").strip()
        )
        if not instance_root:
            raise SystemExit(
                "xAI OAuth needs an instance location. Pass --config <instance_config.yaml> or "
                "--instance-root <directory>."
            )
        instance_root = str(Path(instance_root).expanduser().resolve())
        raw_settings = copy.deepcopy(LLM_DEFAULTS.xai_oauth.value)

    settings = resolve_xai_oauth_settings(
        raw_settings,
        instance_root=instance_root,
    )
    metadata: dict[str, str] = {}
    auth_store_path = str(settings.auth_store_path)
    if getattr(args, "auth_store_path", None):
        auth_store_path = str(Path(args.auth_store_path).expanduser().resolve())
        metadata["auth_store_path_source"] = "command_override"
    elif getattr(args, "host_data_root", None):
        metadata["container_auth_store_path"] = str(settings.auth_store_path)
        metadata["container_data_root"] = str(
            getattr(args, "container_data_root", None) or "/data"
        )
        metadata["host_data_root"] = str(
            Path(args.host_data_root).expanduser().resolve()
        )
        try:
            auth_store_path = _map_container_path_to_host(
                str(settings.auth_store_path),
                host_data_root=args.host_data_root,
                container_data_root=getattr(args, "container_data_root", None)
                or "/data",
            )
        except ValueError as exc:
            raise SystemExit(str(exc)) from exc
        metadata["auth_store_path_source"] = "host_data_root_translation"
    if auth_store_path != settings.auth_store_path:
        settings = settings.__class__(
            base_url=settings.base_url,
            auth_store_path=auth_store_path,
            refresh_skew_seconds=settings.refresh_skew_seconds,
            refresh_timeout_seconds=settings.refresh_timeout_seconds,
        )
    metadata["auth_store_path"] = str(settings.auth_store_path)
    return settings, instance_root, metadata


def _xai_oauth_login(args: argparse.Namespace) -> None:
    from custom_agents.xai_oauth_auth import (
        DEFAULT_XAI_OAUTH_MODEL,
        XAIOAuthError,
        poll_xai_device_token,
        request_xai_device_authorization,
        save_xai_oauth_device_tokens,
    )

    if float(args.timeout) <= 0:
        raise SystemExit("--timeout must be greater than zero.")
    settings, instance_root, metadata = (
        _resolve_xai_oauth_command_settings(args)
    )
    try:
        authorization = request_xai_device_authorization(
            settings,
            instance_root=instance_root,
        )
        print("Open this URL to authorize xAI OAuth:")
        print(authorization.verification_uri_complete)
        print(f"Verification code: {authorization.user_code}")
        if not bool(args.no_browser):
            try:
                opened = webbrowser.open(authorization.verification_uri_complete)
            except Exception:
                opened = False
            if not opened:
                print(
                    "The browser could not be opened automatically; use the URL above."
                )
        print(
            f"Waiting up to {min(float(args.timeout), float(authorization.expires_in)):.0f} seconds for approval..."
        )
        token_payload = poll_xai_device_token(
            authorization,
            timeout_seconds=settings.refresh_timeout_seconds,
            max_wait_seconds=float(args.timeout),
        )
        path = save_xai_oauth_device_tokens(
            authorization,
            token_payload,
            settings,
            instance_root=instance_root,
        )
    except XAIOAuthError as exc:
        raise SystemExit(f"xAI OAuth login failed ({exc.code}): {exc}") from exc

    print(f"xAI OAuth login succeeded. Auth store: {path}")
    if metadata.get("container_auth_store_path"):
        print(f"Container auth store path: {metadata['container_auth_store_path']}")
    print(f"Use provider xai-oauth with model {DEFAULT_XAI_OAUTH_MODEL}.")


def _xai_oauth_status(args: argparse.Namespace) -> None:
    from custom_agents.xai_oauth_auth import xai_oauth_status

    settings, instance_root, metadata = (
        _resolve_xai_oauth_command_settings(args)
    )
    payload = xai_oauth_status(
        settings,
        instance_root=instance_root,
        refresh=bool(args.refresh),
    )
    payload.update(metadata)
    if args.json:
        print(json.dumps(payload, indent=2, sort_keys=True))
        return
    if payload["authenticated"]:
        print(f"xAI OAuth credentials are available from {payload['source']}.")
        print(f"Auth store: {payload['auth_store_path']}")
        if payload.get("container_auth_store_path"):
            print(f"Container auth store: {payload['container_auth_store_path']}")
        return
    print(f"xAI OAuth credentials are unavailable: {payload['message']}")


def _parse_headless_startup_notices(raw: str | None) -> list[str]:
    payload = str(raw or "").strip()
    if not payload:
        return []
    try:
        decoded = json.loads(payload)
    except json.JSONDecodeError:
        decoded = payload
    if isinstance(decoded, list):
        notices: list[str] = []
        for item in decoded:
            rendered = str(item or "").strip()
            if rendered:
                notices.append(rendered)
        return notices
    rendered = str(decoded or "").strip()
    return [rendered] if rendered else []


def _consume_headless_startup_notices() -> list[str]:
    return _parse_headless_startup_notices(
        os.environ.pop(HEADLESS_STARTUP_NOTICE_ENV, None)
    )


def _load_memory_graph_repository(
    config_path: str,
    *,
    env_path: str | None = None,
) -> tuple[GatewayInstanceConfig, Any]:
    config = _load_gateway_instance_config(config_path, env_path=env_path)
    storage_settings = config.resolve_manager_storage_settings(env=os.environ)
    return config, create_memory_graph_repository(storage_settings, logger=logger)


def _load_objective_repository(
    config_path: str,
    *,
    env_path: str | None = None,
) -> tuple[GatewayInstanceConfig, Any]:
    config = _load_gateway_instance_config(config_path, env_path=env_path)
    storage_settings = config.resolve_manager_storage_settings(env=os.environ)
    return config, create_objective_repository(storage_settings, logger=logger)


def _resolve_objective_owner_agent_id(
    config: GatewayInstanceConfig, owner_agent_id: str | None
) -> str:
    explicit_owner = str(owner_agent_id or "").strip()
    resolved = explicit_owner or manager_durable_scope_id(
        team_id=config.manager.team_id,
        named_agent_type=config.manager.named_agent_type,
        fallback_agent_id=config.manager.manager_agent_id,
    )
    if not resolved:
        raise SystemExit(
            "Objective commands require a configured manager team/type or --owner-agent-id."
        )
    return resolved


def _current_embedding_profile_spec(config: GatewayInstanceConfig) -> dict[str, Any]:
    return build_embedding_profile_spec(
        {
            "provider": config.embedding.provider or EMBEDDING_DEFAULTS.provider.value,
            "model_id": config.embedding.model_id,
            "api_base": config.embedding.api_base,
            "dimensions": config.embedding.dimensions,
        }
    ).to_dict()


def _prompt_optional_path(
    prompt: str, *, current: Optional[str] = None
) -> Optional[str]:
    existing = (current or "").strip()
    if existing:
        raw = input(
            f"{prompt} [{existing}] (press Enter to keep, '-' to skip): "
        ).strip()
        if not raw:
            return existing
    else:
        raw = input(f"{prompt} (optional, press Enter to skip): ").strip()
        if not raw:
            return None
    if raw in {"-", "none", "skip"}:
        return None
    return raw


def _validate_onboarding_source_env_file(
    source_env_file_path: str | None,
) -> Optional[str]:
    if not source_env_file_path:
        return None
    env_path = Path(source_env_file_path).expanduser().resolve()
    if not env_path.exists():
        raise FileNotFoundError(f"No such file: {env_path}")
    if not env_path.is_file():
        raise ValueError(f"Not a file: {env_path}")
    return str(env_path)


def _resolve_onboarding_template(args: argparse.Namespace) -> OnboardingTemplate:
    return _resolve_onboarding_seed(args).template


class BaseConfigIsInstanceConfigError(ValueError):
    """--base-config named an instance_config.yaml; the message says what to use instead."""


def _load_onboarding_template(base_config_path: str | None) -> OnboardingTemplate:
    """The base config.yaml's template; an instance_config.yaml is refused in plain words."""

    if base_config_path and _is_instance_config_file(base_config_path):
        raise BaseConfigIsInstanceConfigError(
            f"{base_config_path} is an instance_config.yaml, but --base-config takes a config.yaml. "
            "To carry an old instance's settings into a new one, use the web onboarding's "
            "\"I'm migrating from another UA instance\" import; to change an existing instance, "
            "run onboard again with --instance-root pointing at it."
        )
    return load_base_onboarding_template(base_config_path)


def _is_instance_config_file(path: str) -> bool:
    try:
        raw = yaml.safe_load(Path(path).expanduser().read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError):
        # The full loader reports unreadable files with its own message.
        return False
    return isinstance(raw, dict) and looks_like_instance_config(raw, Path(path).name)


def _resolve_onboarding_seed(args: argparse.Namespace) -> GatewayOnboardingSeed:
    if args.non_interactive:
        try:
            template = _load_onboarding_template(args.base_config)
        except BaseConfigIsInstanceConfigError as exc:
            raise SystemExit(str(exc)) from exc
        return GatewayOnboardingSeed(
            template=template,
            source_env_file_path=_validate_onboarding_source_env_file(
                args.base_env_file
            ),
        )

    print("Onboarding wizard")
    print(
        "This writes runtime settings to instance_config.yaml and editable guidance to workspace markdown files."
    )
    while True:
        base_config_path = _prompt_optional_path(
            "Existing config.yaml to copy from",
            current=args.base_config,
        )
        try:
            template = _load_onboarding_template(base_config_path)
            break
        except Exception as exc:
            print(f"Unable to load config.yaml from {base_config_path}: {exc}")
    if getattr(args, "secrets_from_environment", False):
        # No .env is copied anywhere when secrets come from the environment.
        return GatewayOnboardingSeed(template=template, source_env_file_path=None)
    while True:
        source_env_file_path = _prompt_optional_path(
            "Existing .env file to copy for runtime launch",
            current=args.base_env_file,
        )
        try:
            return GatewayOnboardingSeed(
                template=template,
                source_env_file_path=_validate_onboarding_source_env_file(
                    source_env_file_path
                ),
            )
        except Exception as exc:
            print(f"Unable to use .env file from {source_env_file_path}: {exc}")


def _migrate_instance_config_interactive(config_path: str) -> None:
    status = inspect_instance_config_version(config_path)
    if not status.needs_migration:
        return
    if not sys.stdin.isatty() or not sys.stdout.isatty():
        raise SystemExit(
            f"{status.path} is schema_version {status.detected_version}, but latest is "
            f"{status.latest_version}. Run `assistant_cli.py migrate-config --config {status.path}` first."
        )
    raw = (
        input(
            f"Instance config {status.path} is schema_version {status.detected_version}; latest is "
            f"{status.latest_version}. Migrate now? [Y/n]: "
        )
        .strip()
        .lower()
    )
    if raw not in {"", "y", "yes"}:
        raise SystemExit("Instance config migration declined.")
    result = migrate_instance_config_file(status.path)
    print(
        f"Migrated {result.path} from schema_version {result.original_version} to {result.migrated_version}."
    )
    if result.backup_path is not None:
        print(f"Backup written to {result.backup_path}")
    for step in result.applied_steps:
        print(f"- {step}")


def _discover_instance_config_path() -> Optional[str]:
    explicit = str(os.getenv("TEAM_INSTANCE_CONFIG", "")).strip()
    if explicit:
        candidate = Path(explicit).expanduser().resolve()
        if candidate.exists():
            return str(candidate)
    instance_root = str(os.getenv("TEAM_INSTANCE_ROOT", "")).strip()
    if not instance_root:
        return None
    candidate = Path(instance_root).expanduser().resolve() / "instance_config.yaml"
    if candidate.exists():
        return str(candidate)
    return None


def _resolve_instance_config_path(config_path: str | None) -> str:
    explicit = str(config_path or "").strip()
    if explicit:
        return explicit
    discovered = _discover_instance_config_path()
    if discovered:
        logger.info(
            "Auto-discovered instance_config.yaml from team instance environment: %s",
            discovered,
        )
        return discovered
    raise SystemExit(
        "Path to instance_config.yaml is required. Pass --config <path>, set TEAM_INSTANCE_CONFIG, "
        "or set TEAM_INSTANCE_ROOT to an onboarded instance directory."
    )


def _ensure_instance_config_ready(config_path: str | None) -> str:
    resolved = _resolve_instance_config_path(config_path)
    _migrate_instance_config_interactive(resolved)
    return resolved


def _prompt_text(prompt: str, *, default: Optional[str] = None) -> str:
    suffix = f" [{default}]" if default not in (None, "") else ""
    raw = input(f"{prompt}{suffix}: ").strip()
    if raw:
        return raw
    return default or ""


def _prompt_bool(prompt: str, *, default: bool) -> bool:
    label = "Y/n" if default else "y/N"
    raw = input(f"{prompt} [{label}]: ").strip().lower()
    if not raw:
        return default
    return raw in {"y", "yes", "true", "1"}


def _prompt_int(
    prompt: str, *, default: int, minimum: int = 0, maximum: int = 10_000
) -> int:
    while True:
        raw = input(f"{prompt} [{default}]: ").strip()
        if not raw:
            return default
        try:
            value = int(raw)
        except ValueError:
            print("Enter a whole number.")
            continue
        if value < minimum or value > maximum:
            print(f"Enter a value between {minimum} and {maximum}.")
            continue
        return value


def _prompt_float(
    prompt: str, *, default: float, minimum: float = 0.0, maximum: float = 10_000.0
) -> float:
    while True:
        raw = input(f"{prompt} [{default:g}]: ").strip()
        if not raw:
            return default
        try:
            value = float(raw)
        except ValueError:
            print("Enter a number.")
            continue
        if value < minimum or value > maximum:
            print(f"Enter a value between {minimum:g} and {maximum:g}.")
            continue
        return value


def _format_gateway_timeout_notice(timeout_seconds: float) -> str:
    rounded = f"{timeout_seconds:g}"
    return (
        f"Timed out after waiting {rounded} seconds for a terminal manager response. "
        "The request may still be running; if a late reply arrives, it will be shown as an async message."
    )


def _collect_onboarding_answers(
    args: argparse.Namespace,
    *,
    template: OnboardingTemplate,
) -> dict[str, object]:
    if args.non_interactive:
        if not args.instance_name:
            raise SystemExit("--instance-name is required with --non-interactive.")
        instance_root = _value_or_default(
            args.instance_root, default_instance_root(args.instance_name)
        )
        broker_internal_base_url = _value_or_default(
            args.secure_tool_broker_internal_base_url,
            template.security_layer.broker_internal_base_url
            or DEFAULT_SECURE_TOOL_BROKER_INTERNAL_BASE_URL,
        )
        public_origin = _value_or_default(
            args.public_origin,
            os.getenv("NETWORK_PUBLIC_ORIGIN")
            or template.network.public_origin
            or NETWORK_DEFAULTS.public_origin.value,
        )
        broker_public_port = _value_or_default(
            args.secure_tool_broker_public_port,
            template.security_layer.broker_public_port
            or template.security_layer.broker_bind_port
            or DEFAULT_SECURE_TOOL_BROKER_BIND_PORT,
        )
        internal_website = template.interactive_apps.internal_website
        signal_template = template.interactive_apps.signal
        photon_template = template.interactive_apps.photon
        model_provider = _value_or_default(args.model_provider, template.model_provider)
        model_id = resolve_onboarding_model_id(
            model_provider,
            args.model_id,
            template_provider=template.model_provider,
            template_model_id=template.model_id,
        )
        return {
            "instance_name": args.instance_name,
            "instance_root": instance_root,
            "team_id": args.team_id or template.team_id,
            "timezone": _value_or_default(args.timezone, template.timezone),
            "user_display_name": _value_or_default(
                args.display_name, GATEWAY_DEFAULTS.user_display_name.value
            ),
            "assistant_name": _value_or_default(
                args.assistant_name, GATEWAY_DEFAULTS.assistant_display_name.value
            ),
            "assistant_purpose": args.assistant_purpose,
            "assistant_response_style": args.assistant_response_style,
            "assistant_autonomy": args.assistant_autonomy,
            "user_role": args.user_role,
            "user_context": args.user_context,
            "default_delivery_channel": _value_or_default(
                args.default_delivery_channel,
                template.delivery.default_target.channel_name,
            ),
            "default_delivery_chat_id": _value_or_default(
                args.default_delivery_chat_id,
                template.delivery.default_target.chat_id,
            ),
            "default_delivery_thread_id": _value_or_default(
                args.default_delivery_thread_id,
                template.delivery.default_target.thread_id,
            ),
            "request_timeout_seconds": _value_or_default(
                args.request_timeout_seconds,
                GATEWAY_DEFAULTS.manager_request_timeout_seconds.value,
            ),
            "quiet_enabled": (
                template.delivery.quiet_hours.enabled
                if args.disable_quiet_hours is None
                else not args.disable_quiet_hours
            ),
            "quiet_start": _value_or_default(
                args.quiet_start_hour,
                template.delivery.quiet_hours.start_hour_local,
            ),
            "quiet_end": _value_or_default(
                args.quiet_end_hour,
                template.delivery.quiet_hours.end_hour_local,
            ),
            "digest_hour": _value_or_default(
                args.digest_hour, template.delivery.digest_delivery_hour_local
            ),
            "heartbeat_enabled": not _value_or_default(args.disable_heartbeat, False),
            "heartbeat_interval_seconds": _value_or_default(
                args.heartbeat_interval_seconds,
                HEARTBEAT_DEFAULTS.interval_seconds.value,
            ),
            "heartbeat_active_hours_enabled": _value_or_default(
                args.heartbeat_active_hours,
                HEARTBEAT_DEFAULTS.active_hours_enabled.value,
            ),
            "heartbeat_active_start": _value_or_default(
                args.heartbeat_active_start_hour,
                HEARTBEAT_DEFAULTS.active_start_hour_local.value,
            ),
            "heartbeat_active_end": _value_or_default(
                args.heartbeat_active_end_hour,
                HEARTBEAT_DEFAULTS.active_end_hour_local.value,
            ),
            "model_provider": model_provider,
            "model_id": model_id,
            "reasoning_effort": _value_or_default(
                args.reasoning_effort, template.reasoning_effort
            ),
            "reasoning_summary": _value_or_default(
                args.reasoning_summary, template.reasoning_summary
            ),
            "litellm_api_mode": _value_or_default(
                args.litellm_api_mode, template.litellm_api_mode
            ),
            "security_layer_mode": _value_or_default(
                args.security_layer_mode, template.security_layer.mode
            ),
            "secure_tool_broker_internal_base_url": broker_internal_base_url,
            "public_origin": public_origin,
            "secure_tool_broker_public_port": broker_public_port,
            "secure_tool_broker_bind_host": _value_or_default(
                args.secure_tool_broker_bind_host,
                template.security_layer.broker_bind_host,
            ),
            "secure_tool_broker_bind_port": _value_or_default(
                args.secure_tool_broker_bind_port,
                template.security_layer.broker_bind_port,
            ),
            "secure_tool_broker_credentials_dir": _value_or_default(
                args.secure_tool_broker_credentials_dir,
                template.security_layer.broker_credentials_dir,
            ),
            "internal_website_bind_host": _value_or_default(
                args.internal_website_bind_host, internal_website.bind_host
            ),
            "internal_website_bind_port": _value_or_default(
                args.internal_website_bind_port, internal_website.bind_port
            ),
            "internal_website_public_port": _value_or_default(
                args.internal_website_public_port, internal_website.public_port
            ),
            "internal_website_docs_root": _value_or_default(
                args.internal_website_docs_root, internal_website.docs_root
            ),
            "internal_website_skills_dir": _value_or_default(
                args.internal_website_skills_dir, internal_website.skills_dir
            ),
            "discord_enabled": _value_or_default(args.enable_discord, False),
            "discord_allowed_user_ids": list(args.discord_allow_user_id or []),
            "discord_allowed_channel_ids": list(args.discord_allow_channel_id or []),
            "discord_group_policy": _value_or_default(
                args.discord_group_policy,
                INTERACTIVE_APP_DEFAULTS.group_policy.value,
            ),
            "discord_thread_scoped_sessions": _value_or_default(
                args.discord_thread_scoped_sessions,
                INTERACTIVE_APP_DEFAULTS.scoped_sessions.value,
            ),
            "discord_command_guild_ids": list(args.discord_command_guild_id or []),
            "telegram_enabled": _value_or_default(args.enable_telegram, False),
            "telegram_allowed_user_ids": list(args.telegram_allow_user_id or []),
            "telegram_allowed_chat_ids": list(args.telegram_allow_chat_id or []),
            "telegram_group_policy": _value_or_default(
                args.telegram_group_policy,
                INTERACTIVE_APP_DEFAULTS.group_policy.value,
            ),
            "telegram_manager_offline_policy": _normalize_telegram_manager_offline_policy(
                _value_or_default(
                    args.telegram_manager_offline_policy,
                    DEFAULT_TELEGRAM_MANAGER_OFFLINE_POLICY,
                )
            ),
            "telegram_topic_scoped_sessions": _value_or_default(
                args.telegram_topic_scoped_sessions,
                INTERACTIVE_APP_DEFAULTS.scoped_sessions.value,
            ),
            "telegram_drop_pending_updates": _value_or_default(
                args.telegram_drop_pending_updates,
                INTERACTIVE_APP_DEFAULTS.drop_pending_updates.value,
            ),
            "signal_enabled": _value_or_default(args.enable_signal, False),
            "signal_base_url": _value_or_default(
                args.signal_base_url,
                signal_template.base_url,
            ),
            "signal_account": _value_or_default(
                args.signal_account, signal_template.account
            ),
            "signal_allowed_sender_ids": list(args.signal_allow_sender_id or []),
            "signal_allowed_group_ids": list(args.signal_allow_group_id or []),
            "signal_group_policy": _value_or_default(
                args.signal_group_policy,
                signal_template.group_policy,
            ),
            "signal_mention_markers": list(
                _value_or_default(
                    args.signal_mention_marker, signal_template.mention_markers
                )
                or []
            ),
            "signal_poll_timeout_seconds": _value_or_default(
                args.signal_poll_timeout_seconds,
                signal_template.poll_timeout_seconds,
            ),
            "signal_max_messages_per_poll": _value_or_default(
                args.signal_max_messages_per_poll,
                signal_template.max_messages_per_poll,
            ),
            "signal_drop_pending_messages_on_startup": _value_or_default(
                args.signal_drop_pending_messages_on_startup,
                signal_template.drop_pending_messages_on_startup,
            ),
            "photon_enabled": _value_or_default(args.enable_photon, False),
            "photon_project_id": _value_or_default(
                args.photon_project_id, photon_template.project_id
            ),
            "photon_project_secret": _value_or_default(args.photon_project_secret, ""),
            "photon_allowed_sender_ids": list(args.photon_allow_sender_id or []),
            "photon_allowed_group_ids": list(args.photon_allow_group_id or []),
            "photon_group_policy": _value_or_default(
                args.photon_group_policy,
                photon_template.group_policy,
            ),
            "photon_mention_markers": list(
                _value_or_default(
                    args.photon_mention_marker, photon_template.mention_markers
                )
                or []
            ),
            "whatsapp_enabled": _value_or_default(args.enable_whatsapp, False),
            "whatsapp_allowed_sender_ids": list(args.whatsapp_allow_sender_id or []),
            "whatsapp_webhook_host": _value_or_default(
                args.whatsapp_webhook_host,
                INTERACTIVE_APP_DEFAULTS.whatsapp_webhook_host.value,
            ),
            "whatsapp_webhook_port": _value_or_default(
                args.whatsapp_webhook_port,
                INTERACTIVE_APP_DEFAULTS.whatsapp_webhook_port.value,
            ),
            "whatsapp_webhook_path": _value_or_default(
                args.whatsapp_webhook_path,
                INTERACTIVE_APP_DEFAULTS.whatsapp_webhook_path.value,
            ),
            "whatsapp_public_base_url": _value_or_default(
                args.whatsapp_public_base_url, ""
            ),
            "whatsapp_validate_twilio_signature": _value_or_default(
                args.whatsapp_validate_twilio_signature,
                INTERACTIVE_APP_DEFAULTS.validate_twilio_signature.value,
            ),
        }

    instance_name = _prompt_text("Instance name", default=args.instance_name or "")
    if not instance_name:
        raise SystemExit("Instance name is required.")
    instance_root_default = _value_or_default(
        args.instance_root, default_instance_root(instance_name)
    )
    timezone = _prompt_text(
        "User timezone", default=_value_or_default(args.timezone, template.timezone)
    )
    user_display_name = _prompt_text(
        "Your display name",
        default=_value_or_default(
            args.display_name, GATEWAY_DEFAULTS.user_display_name.value
        ),
    )
    assistant_name = _prompt_text(
        "Assistant display name",
        default=_value_or_default(
            args.assistant_name, GATEWAY_DEFAULTS.assistant_display_name.value
        ),
    )
    assistant_purpose = _prompt_text(
        "Assistant purpose",
        default=args.assistant_purpose or "Help me solve problems pragmatically.",
    )
    assistant_response_style = _prompt_text(
        "Preferred response style",
        default=args.assistant_response_style or "Concise and direct. Talk to me normally and do not use jargon unless something requires technical precision.",
    )
    assistant_autonomy = _prompt_text(
        "Autonomy preference",
        default=args.assistant_autonomy
        or "Ask clarifying questions when ambiguity or risk is high.",
    )
    user_role = _prompt_text("Your role/title", default=args.user_role or "")
    user_context = _prompt_text(
        "Initial context about you", default=args.user_context or ""
    )
    instance_root = _prompt_text(
        "Instance root directory", default=str(instance_root_default)
    )
    team_id = _prompt_text("Team id", default=args.team_id or template.team_id)
    default_delivery_channel = _prompt_text(
        "Default delivery channel",
        default=_value_or_default(
            args.default_delivery_channel, template.delivery.default_target.channel_name
        ),
    )
    default_delivery_chat_id = _prompt_text(
        "Default delivery chat id",
        default=_value_or_default(
            args.default_delivery_chat_id, template.delivery.default_target.chat_id
        ),
    )
    default_delivery_thread_id = _prompt_text(
        "Default delivery thread/topic id",
        default=_value_or_default(
            args.default_delivery_thread_id, template.delivery.default_target.thread_id
        ),
    )
    request_timeout_seconds = _prompt_int(
        "Gateway request timeout (seconds)",
        default=int(
            _value_or_default(
                args.request_timeout_seconds,
                GATEWAY_DEFAULTS.manager_request_timeout_seconds.value,
            )
        ),
        minimum=1,
        maximum=86_400,
    )
    quiet_enabled = _prompt_bool(
        "Enable quiet hours",
        default=template.delivery.quiet_hours.enabled
        if args.disable_quiet_hours is None
        else not args.disable_quiet_hours,
    )
    quiet_start = _prompt_int(
        "Quiet-hours start hour",
        default=_value_or_default(
            args.quiet_start_hour, template.delivery.quiet_hours.start_hour_local
        ),
        minimum=0,
        maximum=23,
    )
    quiet_end = _prompt_int(
        "Quiet-hours end hour",
        default=_value_or_default(
            args.quiet_end_hour, template.delivery.quiet_hours.end_hour_local
        ),
        minimum=0,
        maximum=23,
    )
    digest_hour = _prompt_int(
        "Digest release hour",
        default=_value_or_default(
            args.digest_hour, template.delivery.digest_delivery_hour_local
        ),
        minimum=0,
        maximum=23,
    )
    heartbeat_enabled = _prompt_bool(
        "Enable heartbeat", default=not _value_or_default(args.disable_heartbeat, False)
    )
    heartbeat_interval_seconds = _prompt_int(
        "Heartbeat interval (seconds)",
        default=_value_or_default(
            args.heartbeat_interval_seconds, HEARTBEAT_DEFAULTS.interval_seconds.value
        ),
        minimum=1,
        maximum=86_400,
    )
    heartbeat_active_hours_enabled = _prompt_bool(
        "Restrict heartbeat to active hours",
        default=_value_or_default(
            args.heartbeat_active_hours, HEARTBEAT_DEFAULTS.active_hours_enabled.value
        ),
    )
    heartbeat_active_start = _prompt_int(
        "Heartbeat active-hours start",
        default=_value_or_default(
            args.heartbeat_active_start_hour,
            HEARTBEAT_DEFAULTS.active_start_hour_local.value,
        ),
        minimum=0,
        maximum=23,
    )
    heartbeat_active_end = _prompt_int(
        "Heartbeat active-hours end",
        default=_value_or_default(
            args.heartbeat_active_end_hour,
            HEARTBEAT_DEFAULTS.active_end_hour_local.value,
        ),
        minimum=0,
        maximum=23,
    )
    model_provider = _prompt_text(
        "Preferred model provider",
        default=_value_or_default(args.model_provider, template.model_provider),
    )
    model_id = _prompt_text(
        "Preferred model id",
        default=resolve_onboarding_model_id(
            model_provider,
            args.model_id,
            template_provider=template.model_provider,
            template_model_id=template.model_id,
        ),
    )
    reasoning_effort = _prompt_text(
        "Preferred reasoning effort",
        default=_value_or_default(args.reasoning_effort, template.reasoning_effort),
    )
    reasoning_summary = _prompt_text(
        "Reasoning summary (blank, auto, concise, or detailed)",
        default=_value_or_default(
            args.reasoning_summary, template.reasoning_summary or ""
        ),
    )
    reasoning_summary = normalize_reasoning_summary(reasoning_summary)
    litellm_api_mode = _prompt_text(
        "LiteLLM API mode (chat_completions or responses)",
        default=_value_or_default(args.litellm_api_mode, template.litellm_api_mode),
    )
    litellm_api_mode = normalize_litellm_api_mode(litellm_api_mode)
    security_layer_mode = normalize_security_layer_mode(
        _prompt_text(
            "Security mode (broker, or openshell to also sandbox agent commands)",
            default=_value_or_default(
                args.security_layer_mode, template.security_layer.mode
            ),
        )
    )
    public_origin = _prompt_text(
        "Public origin",
        default=_value_or_default(
            args.public_origin,
            os.getenv("NETWORK_PUBLIC_ORIGIN")
            or template.network.public_origin
            or NETWORK_DEFAULTS.public_origin.value,
        ),
    )
    secure_tool_broker_internal_base_url = _prompt_text(
        "Secure tool broker internal base URL",
        default=_value_or_default(
            args.secure_tool_broker_internal_base_url,
            template.security_layer.broker_internal_base_url
            or DEFAULT_SECURE_TOOL_BROKER_INTERNAL_BASE_URL,
        ),
    )
    secure_tool_broker_public_port = _prompt_int(
        "Secure tool broker public port",
        default=int(
            _value_or_default(
                args.secure_tool_broker_public_port,
                template.security_layer.broker_public_port
                or template.security_layer.broker_bind_port
                or DEFAULT_SECURE_TOOL_BROKER_BIND_PORT,
            )
        ),
        minimum=1,
        maximum=65_535,
    )
    secure_tool_broker_bind_host = _prompt_text(
        "Secure tool broker bind host",
        default=_value_or_default(
            args.secure_tool_broker_bind_host,
            template.security_layer.broker_bind_host or "",
        ),
    )
    secure_tool_broker_bind_port = _prompt_int(
        "Secure tool broker bind port",
        default=int(
            _value_or_default(
                args.secure_tool_broker_bind_port,
                template.security_layer.broker_bind_port
                or DEFAULT_SECURE_TOOL_BROKER_BIND_PORT,
            )
        ),
        minimum=1,
        maximum=65_535,
    )
    secure_tool_broker_credentials_dir = _prompt_text(
        "Secure tool broker credential state directory",
        default=_value_or_default(
            args.secure_tool_broker_credentials_dir,
            template.security_layer.broker_credentials_dir or "broker_credentials",
        ),
    )
    internal_website_template = template.interactive_apps.internal_website
    internal_website_bind_host = _prompt_text(
        "Internal website bind host",
        default=_value_or_default(
            args.internal_website_bind_host, internal_website_template.bind_host
        ),
    )
    internal_website_bind_port = _prompt_int(
        "Internal website bind port",
        default=int(
            _value_or_default(
                args.internal_website_bind_port, internal_website_template.bind_port
            )
        ),
        minimum=1,
        maximum=65_535,
    )
    internal_website_public_port = _prompt_int(
        "Internal website public port",
        default=int(
            _value_or_default(
                args.internal_website_public_port,
                internal_website_template.public_port
                or INTERACTIVE_APP_DEFAULTS.internal_website_bind_port.value,
            )
        ),
        minimum=1,
        maximum=65_535,
    )
    internal_website_docs_root = _prompt_text(
        "Internal website docs root",
        default=_value_or_default(
            args.internal_website_docs_root, internal_website_template.docs_root or ""
        ),
    )
    internal_website_skills_dir = _prompt_text(
        "Internal website skills docs directory",
        default=_value_or_default(
            args.internal_website_skills_dir, internal_website_template.skills_dir or ""
        ),
    )
    discord_enabled = _prompt_bool(
        "Enable Discord app", default=_value_or_default(args.enable_discord, False)
    )
    discord_allowed_user_ids = list(args.discord_allow_user_id or [])
    discord_allowed_channel_ids = list(args.discord_allow_channel_id or [])
    discord_group_policy = _value_or_default(
        args.discord_group_policy, INTERACTIVE_APP_DEFAULTS.group_policy.value
    )
    discord_thread_scoped_sessions = _value_or_default(
        args.discord_thread_scoped_sessions,
        INTERACTIVE_APP_DEFAULTS.scoped_sessions.value,
    )
    discord_command_guild_ids = list(args.discord_command_guild_id or [])
    if discord_enabled:
        raw_allowed_discord_users = _prompt_text(
            "Allowed Discord user ids (comma-separated, blank for any)",
            default=",".join(discord_allowed_user_ids),
        )
        raw_allowed_discord_channels = _prompt_text(
            "Allowed Discord channel ids (comma-separated, blank for any)",
            default=",".join(discord_allowed_channel_ids),
        )
        discord_group_policy = _prompt_text(
            "Discord group policy (open or mention)",
            default=discord_group_policy,
        )
        discord_thread_scoped_sessions = _prompt_bool(
            "Scope Discord sessions by thread",
            default=discord_thread_scoped_sessions,
        )
        raw_discord_command_guild_ids = _prompt_text(
            "Discord command guild ids for immediate slash-command sync (comma-separated, blank for global-only)",
            default=",".join(discord_command_guild_ids),
        )
        discord_allowed_user_ids = [
            value.strip()
            for value in raw_allowed_discord_users.split(",")
            if value.strip()
        ]
        discord_allowed_channel_ids = [
            value.strip()
            for value in raw_allowed_discord_channels.split(",")
            if value.strip()
        ]
        discord_command_guild_ids = [
            value.strip()
            for value in raw_discord_command_guild_ids.split(",")
            if value.strip()
        ]
    telegram_enabled = _prompt_bool(
        "Enable Telegram app", default=_value_or_default(args.enable_telegram, False)
    )
    telegram_allowed_user_ids = list(args.telegram_allow_user_id or [])
    telegram_allowed_chat_ids = list(args.telegram_allow_chat_id or [])
    telegram_group_policy = _value_or_default(
        args.telegram_group_policy, INTERACTIVE_APP_DEFAULTS.group_policy.value
    )
    telegram_manager_offline_policy = _normalize_telegram_manager_offline_policy(
        _value_or_default(
            args.telegram_manager_offline_policy,
            DEFAULT_TELEGRAM_MANAGER_OFFLINE_POLICY,
        )
    )
    telegram_topic_scoped_sessions = _value_or_default(
        args.telegram_topic_scoped_sessions,
        INTERACTIVE_APP_DEFAULTS.scoped_sessions.value,
    )
    telegram_drop_pending_updates = _value_or_default(
        args.telegram_drop_pending_updates,
        INTERACTIVE_APP_DEFAULTS.drop_pending_updates.value,
    )
    if telegram_enabled:
        raw_allowed_users = _prompt_text(
            "Allowed Telegram user ids (comma-separated, blank for any)",
            default=",".join(telegram_allowed_user_ids),
        )
        raw_allowed_chats = _prompt_text(
            "Allowed Telegram chat ids (comma-separated, blank for any)",
            default=",".join(telegram_allowed_chat_ids),
        )
        telegram_group_policy = _prompt_text(
            "Telegram group policy (open or mention)",
            default=telegram_group_policy,
        )
        telegram_manager_offline_policy = _normalize_telegram_manager_offline_policy(
            _prompt_text(
                "Telegram manager-offline policy (notify or pause_telegram_polling)",
                default=telegram_manager_offline_policy,
            )
        )
        telegram_topic_scoped_sessions = _prompt_bool(
            "Scope Telegram sessions by topic/thread",
            default=telegram_topic_scoped_sessions,
        )
        telegram_drop_pending_updates = _prompt_bool(
            "Drop pending Telegram updates on startup",
            default=telegram_drop_pending_updates,
        )
        telegram_allowed_user_ids = [
            value.strip() for value in raw_allowed_users.split(",") if value.strip()
        ]
        telegram_allowed_chat_ids = [
            value.strip() for value in raw_allowed_chats.split(",") if value.strip()
        ]
    signal_template = template.interactive_apps.signal
    signal_enabled = _prompt_bool(
        "Enable Signal app", default=_value_or_default(args.enable_signal, False)
    )
    signal_base_url = _value_or_default(args.signal_base_url, signal_template.base_url)
    signal_account = _value_or_default(args.signal_account, signal_template.account)
    signal_allowed_sender_ids = list(args.signal_allow_sender_id or [])
    signal_allowed_group_ids = list(args.signal_allow_group_id or [])
    signal_group_policy = _value_or_default(
        args.signal_group_policy, signal_template.group_policy
    )
    signal_mention_markers = list(
        _value_or_default(args.signal_mention_marker, signal_template.mention_markers)
        or []
    )
    signal_poll_timeout_seconds = _value_or_default(
        args.signal_poll_timeout_seconds,
        signal_template.poll_timeout_seconds,
    )
    signal_max_messages_per_poll = _value_or_default(
        args.signal_max_messages_per_poll,
        signal_template.max_messages_per_poll,
    )
    signal_drop_pending_messages_on_startup = _value_or_default(
        args.signal_drop_pending_messages_on_startup,
        signal_template.drop_pending_messages_on_startup,
    )
    if signal_enabled:
        signal_base_url = _prompt_text(
            "Signal REST API base URL", default=signal_base_url
        )
        signal_account = _prompt_text("Signal account/number", default=signal_account)
        raw_signal_allowed_senders = _prompt_text(
            "Allowed Signal direct sender ids or phone numbers (comma-separated, blank for any)",
            default=",".join(signal_allowed_sender_ids),
        )
        raw_signal_allowed_groups = _prompt_text(
            "Allowed Signal group ids (comma-separated, blank to disable groups)",
            default=",".join(signal_allowed_group_ids),
        )
        signal_group_policy = _prompt_text(
            "Signal group policy (open or mention)",
            default=signal_group_policy,
        )
        default_signal_marker_text = (
            ",".join(signal_mention_markers) or f"@{assistant_name}"
        )
        raw_signal_mention_markers = _prompt_text(
            "Signal group mention markers (comma-separated)",
            default=default_signal_marker_text,
        )
        signal_poll_timeout_seconds = _prompt_float(
            "Signal receive long-poll timeout seconds",
            default=float(signal_poll_timeout_seconds),
            minimum=0.1,
        )
        signal_max_messages_per_poll = _prompt_int(
            "Signal max messages per poll",
            default=int(signal_max_messages_per_poll),
            minimum=1,
        )
        signal_drop_pending_messages_on_startup = _prompt_bool(
            "Drop pending Signal messages on startup",
            default=bool(signal_drop_pending_messages_on_startup),
        )
        signal_allowed_sender_ids = [
            value.strip()
            for value in raw_signal_allowed_senders.split(",")
            if value.strip()
        ]
        signal_allowed_group_ids = [
            value.strip()
            for value in raw_signal_allowed_groups.split(",")
            if value.strip()
        ]
        signal_mention_markers = [
            value.strip()
            for value in raw_signal_mention_markers.split(",")
            if value.strip()
        ]
    photon_template = template.interactive_apps.photon
    photon_enabled = _prompt_bool(
        "Enable Photon iMessage app",
        default=_value_or_default(args.enable_photon, False),
    )
    photon_project_id = _value_or_default(
        args.photon_project_id, photon_template.project_id
    )
    photon_project_secret = _value_or_default(args.photon_project_secret, "")
    photon_allowed_sender_ids = list(args.photon_allow_sender_id or [])
    photon_allowed_group_ids = list(args.photon_allow_group_id or [])
    photon_group_policy = _value_or_default(
        args.photon_group_policy, photon_template.group_policy
    )
    photon_mention_markers = list(
        _value_or_default(args.photon_mention_marker, photon_template.mention_markers)
        or []
    )
    if photon_enabled:
        photon_project_id = _prompt_text(
            "Photon PHOTON_PROJECT_ID", default=photon_project_id
        )
        photon_project_secret = _prompt_text(
            "Photon PHOTON_PROJECT_SECRET", default=photon_project_secret
        )
        raw_photon_allowed_senders = _prompt_text(
            "Allowed Photon direct sender ids (comma-separated, blank for any)",
            default=",".join(photon_allowed_sender_ids),
        )
        raw_photon_allowed_groups = _prompt_text(
            "Allowed Photon group ids (comma-separated, blank to disable groups)",
            default=",".join(photon_allowed_group_ids),
        )
        photon_group_policy = _prompt_text(
            "Photon group policy (open or mention)",
            default=photon_group_policy,
        )
        default_photon_marker_text = (
            ",".join(photon_mention_markers) or f"@{assistant_name}"
        )
        raw_photon_mention_markers = _prompt_text(
            "Photon group mention markers (comma-separated)",
            default=default_photon_marker_text,
        )
        photon_allowed_sender_ids = [
            value.strip()
            for value in raw_photon_allowed_senders.split(",")
            if value.strip()
        ]
        photon_allowed_group_ids = [
            value.strip()
            for value in raw_photon_allowed_groups.split(",")
            if value.strip()
        ]
        photon_mention_markers = [
            value.strip()
            for value in raw_photon_mention_markers.split(",")
            if value.strip()
        ]
    whatsapp_enabled = _prompt_bool(
        "Enable WhatsApp app", default=_value_or_default(args.enable_whatsapp, False)
    )
    whatsapp_allowed_sender_ids = list(args.whatsapp_allow_sender_id or [])
    whatsapp_webhook_host = _value_or_default(
        args.whatsapp_webhook_host,
        INTERACTIVE_APP_DEFAULTS.whatsapp_webhook_host.value,
    )
    whatsapp_webhook_port = _value_or_default(
        args.whatsapp_webhook_port,
        INTERACTIVE_APP_DEFAULTS.whatsapp_webhook_port.value,
    )
    whatsapp_webhook_path = _value_or_default(
        args.whatsapp_webhook_path,
        INTERACTIVE_APP_DEFAULTS.whatsapp_webhook_path.value,
    )
    whatsapp_public_base_url = _value_or_default(args.whatsapp_public_base_url, "")
    whatsapp_validate_twilio_signature = _value_or_default(
        args.whatsapp_validate_twilio_signature,
        INTERACTIVE_APP_DEFAULTS.validate_twilio_signature.value,
    )
    if whatsapp_enabled:
        raw_whatsapp_allowed_senders = _prompt_text(
            "Allowed WhatsApp sender ids or phone numbers (comma-separated, blank for any)",
            default=",".join(whatsapp_allowed_sender_ids),
        )
        whatsapp_webhook_host = _prompt_text(
            "WhatsApp webhook host/interface",
            default=whatsapp_webhook_host,
        )
        whatsapp_webhook_port = _prompt_int(
            "WhatsApp webhook port",
            default=whatsapp_webhook_port,
            minimum=1,
            maximum=65_535,
        )
        whatsapp_webhook_path = _prompt_text(
            "WhatsApp webhook path",
            default=whatsapp_webhook_path,
        )
        whatsapp_public_base_url = _prompt_text(
            "WhatsApp public webhook base URL (blank to skip)",
            default=whatsapp_public_base_url,
        )
        whatsapp_validate_twilio_signature = _prompt_bool(
            "Validate Twilio webhook signatures",
            default=whatsapp_validate_twilio_signature,
        )
        whatsapp_allowed_sender_ids = [
            value.strip()
            for value in raw_whatsapp_allowed_senders.split(",")
            if value.strip()
        ]

    return {
        "instance_name": instance_name,
        "instance_root": instance_root,
        "team_id": team_id,
        "timezone": timezone,
        "user_display_name": user_display_name,
        "assistant_name": assistant_name,
        "assistant_purpose": assistant_purpose,
        "assistant_response_style": assistant_response_style,
        "assistant_autonomy": assistant_autonomy,
        "user_role": user_role,
        "user_context": user_context,
        "default_delivery_channel": default_delivery_channel,
        "default_delivery_chat_id": default_delivery_chat_id,
        "default_delivery_thread_id": default_delivery_thread_id,
        "request_timeout_seconds": request_timeout_seconds,
        "quiet_enabled": quiet_enabled,
        "quiet_start": quiet_start,
        "quiet_end": quiet_end,
        "digest_hour": digest_hour,
        "heartbeat_enabled": heartbeat_enabled,
        "heartbeat_interval_seconds": heartbeat_interval_seconds,
        "heartbeat_active_hours_enabled": heartbeat_active_hours_enabled,
        "heartbeat_active_start": heartbeat_active_start,
        "heartbeat_active_end": heartbeat_active_end,
        "model_provider": model_provider,
        "model_id": model_id,
        "reasoning_effort": reasoning_effort,
        "reasoning_summary": reasoning_summary,
        "litellm_api_mode": litellm_api_mode,
        "security_layer_mode": security_layer_mode,
        "secure_tool_broker_internal_base_url": secure_tool_broker_internal_base_url,
        "public_origin": public_origin,
        "secure_tool_broker_public_port": secure_tool_broker_public_port,
        "secure_tool_broker_bind_host": secure_tool_broker_bind_host,
        "secure_tool_broker_bind_port": secure_tool_broker_bind_port,
        "secure_tool_broker_credentials_dir": secure_tool_broker_credentials_dir,
        "internal_website_bind_host": internal_website_bind_host,
        "internal_website_bind_port": internal_website_bind_port,
        "internal_website_public_port": internal_website_public_port,
        "internal_website_docs_root": internal_website_docs_root,
        "internal_website_skills_dir": internal_website_skills_dir,
        "discord_enabled": discord_enabled,
        "discord_allowed_user_ids": discord_allowed_user_ids,
        "discord_allowed_channel_ids": discord_allowed_channel_ids,
        "discord_group_policy": discord_group_policy,
        "discord_thread_scoped_sessions": discord_thread_scoped_sessions,
        "discord_command_guild_ids": discord_command_guild_ids,
        "telegram_enabled": telegram_enabled,
        "telegram_allowed_user_ids": telegram_allowed_user_ids,
        "telegram_allowed_chat_ids": telegram_allowed_chat_ids,
        "telegram_group_policy": telegram_group_policy,
        "telegram_manager_offline_policy": telegram_manager_offline_policy,
        "telegram_topic_scoped_sessions": telegram_topic_scoped_sessions,
        "telegram_drop_pending_updates": telegram_drop_pending_updates,
        "signal_enabled": signal_enabled,
        "signal_base_url": signal_base_url,
        "signal_account": signal_account,
        "signal_allowed_sender_ids": signal_allowed_sender_ids,
        "signal_allowed_group_ids": signal_allowed_group_ids,
        "signal_group_policy": signal_group_policy,
        "signal_mention_markers": signal_mention_markers,
        "signal_poll_timeout_seconds": signal_poll_timeout_seconds,
        "signal_max_messages_per_poll": signal_max_messages_per_poll,
        "signal_drop_pending_messages_on_startup": signal_drop_pending_messages_on_startup,
        "photon_enabled": photon_enabled,
        "photon_project_id": photon_project_id,
        "photon_project_secret": photon_project_secret,
        "photon_allowed_sender_ids": photon_allowed_sender_ids,
        "photon_allowed_group_ids": photon_allowed_group_ids,
        "photon_group_policy": photon_group_policy,
        "photon_mention_markers": photon_mention_markers,
        "whatsapp_enabled": whatsapp_enabled,
        "whatsapp_allowed_sender_ids": whatsapp_allowed_sender_ids,
        "whatsapp_webhook_host": whatsapp_webhook_host,
        "whatsapp_webhook_port": whatsapp_webhook_port,
        "whatsapp_webhook_path": whatsapp_webhook_path,
        "whatsapp_public_base_url": whatsapp_public_base_url,
        "whatsapp_validate_twilio_signature": whatsapp_validate_twilio_signature,
    }


def _use_gateway_broker_token() -> None:
    """A gateway started by hand holds the broker's root secret from the env
    file; the broker accepts only this process's derived gateway token
    (access_control.caller_tokens). Under the supervisor the token is already
    derived and is kept."""

    from access_control.caller_tokens import CALLER_GATEWAY, with_own_caller_token

    with_own_caller_token(os.environ, CALLER_GATEWAY)  # type: ignore[arg-type]


async def _chat(
    config_path: str, session_key: str | None, *, env_path: str | None = None
) -> None:
    config = _load_gateway_instance_config(config_path, env_path=env_path)
    _use_gateway_broker_token()
    tracker = CliMessageTracker()
    console = InteractiveCliConsole()
    cli_channel_name = "cli"
    cli_user_id = config.user.canonical_user_id
    cli_chat_id = f"cli:{config.instance_name}"

    def _print_response(response: GatewayApplicationResponse) -> None:
        ref = tracker.add(
            text=response.text,
            session_key=None,
            conversation_id=response.conversation_id,
        )
        console.print_message(
            ref_id=ref.ref_id,
            conversation_id=response.conversation_id,
            text=response.text,
        )

    async def event_sink(event: GatewayDeliveryEvent) -> None:
        ref = tracker.add(
            text=event.text,
            session_key=event.session_key,
            conversation_id=event.conversation_id,
        )
        console.print_message(
            ref_id=ref.ref_id,
            conversation_id=event.conversation_id,
            text=event.text,
        )

    async def query_handler(task_query) -> str:
        return await console.read_input(f"[Clarification] {task_query.message}\n> ")

    async with UserGateway(config, output_sink=None, event_sink=event_sink) as gateway:
        current_session = gateway.application.resolve_session(
            session_key=session_key,
            channel_name=cli_channel_name,
            user_id=cli_user_id,
            chat_id=cli_chat_id,
        )
        current_session_key = current_session.session_key
        print(
            "Interactive gateway chat started. Type /exit to quit.\n"
            f"{INTERACTIVE_SESSION_HELP}\n"
            f"Current session: {current_session_key}\n"
            f"Active conversation: {current_session.conversation_id or '-'}"
        )
        while True:
            user_text = await console.read_input()
            if user_text.strip().lower() in {"/exit", "/quit"}:
                break
            if not user_text.strip():
                continue
            reply_ref, reply_text = _parse_reply_command(user_text)
            if reply_ref and reply_text:
                ref = tracker.get(reply_ref)
                if ref is None or not ref.conversation_id:
                    console.print_text(
                        f"Message reference not found or has no conversation: {reply_ref}"
                    )
                    continue
                try:
                    response = await gateway.application.send_user_message(
                        reply_text,
                        session_key=current_session_key,
                        conversation_id=ref.conversation_id,
                        channel_name=cli_channel_name,
                        user_id=cli_user_id,
                        chat_id=cli_chat_id,
                        query_handler=query_handler,
                    )
                    _print_response(response)
                except TimeoutError:
                    console.print_text(
                        _format_gateway_timeout_notice(
                            config.manager.request_timeout_seconds
                        )
                    )
                continue
            handled, current_session_key, message = await _handle_local_session_command(
                raw_text=user_text,
                application=gateway.application,
                current_session_key=current_session_key,
                channel_name=cli_channel_name,
                user_id=cli_user_id,
                chat_id=cli_chat_id,
                query_handler=query_handler,
            )
            if handled:
                if message:
                    console.print_text(message)
                continue
            try:
                response = await gateway.application.send_user_message(
                    user_text,
                    session_key=current_session_key,
                    channel_name=cli_channel_name,
                    user_id=cli_user_id,
                    chat_id=cli_chat_id,
                    query_handler=query_handler,
                )
                _print_response(response)
            except TimeoutError:
                console.print_text(
                    _format_gateway_timeout_notice(
                        config.manager.request_timeout_seconds
                    )
                )


async def _bridge_chat(
    config_path: str,
    session_key: str | None,
    channel_name: str,
    chat_id: str,
    user_id: str,
    *,
    env_path: str | None = None,
) -> None:
    config = _load_gateway_instance_config(config_path, env_path=env_path)
    tracker = CliMessageTracker()
    console = InteractiveCliConsole()

    async def event_sink(event: GatewayDeliveryEvent) -> None:
        ref = tracker.add(
            text=event.text,
            session_key=event.session_key,
            conversation_id=event.conversation_id,
        )
        console.print_message(
            ref_id=ref.ref_id,
            conversation_id=event.conversation_id,
            text=event.text,
        )

    async with UserGateway(config, output_sink=None, event_sink=event_sink) as gateway:
        bridge = ChannelBridge(gateway.application)
        current_session = gateway.application.resolve_session(
            session_key=session_key,
            channel_name=channel_name,
            user_id=user_id,
            chat_id=chat_id,
        )
        current_session_key = current_session.session_key
        print(
            "Interactive bridge chat started. Type /exit to quit.\n"
            f"{INTERACTIVE_SESSION_HELP}\n"
            f"Current session: {current_session_key}\n"
            f"Active conversation: {current_session.conversation_id or '-'}"
        )
        while True:
            user_text = await console.read_input()
            if user_text.strip().lower() in {"/exit", "/quit"}:
                break
            reply_ref, reply_text = _parse_reply_command(user_text)
            if reply_ref and reply_text:
                ref = tracker.get(reply_ref)
                if ref is None or not ref.conversation_id:
                    console.print_text(
                        f"Message reference not found or has no conversation: {reply_ref}"
                    )
                    continue
                try:
                    response = await gateway.application.send_user_message(
                        reply_text,
                        session_key=current_session_key,
                        conversation_id=ref.conversation_id,
                        channel_name=channel_name,
                        user_id=user_id,
                        chat_id=chat_id,
                    )
                    ref_record = tracker.add(
                        text=response.text,
                        session_key=current_session_key,
                        conversation_id=response.conversation_id,
                    )
                    console.print_message(
                        ref_id=ref_record.ref_id,
                        conversation_id=response.conversation_id,
                        text=response.text,
                    )
                except TimeoutError:
                    console.print_text(
                        _format_gateway_timeout_notice(
                            config.manager.request_timeout_seconds
                        )
                    )
                continue
            handled, current_session_key, message = await _handle_local_session_command(
                raw_text=user_text,
                application=gateway.application,
                current_session_key=current_session_key,
                channel_name=channel_name,
                user_id=user_id,
                chat_id=chat_id,
            )
            if handled:
                if message:
                    console.print_text(message)
                continue
            inbound = ChannelInboundMessage(
                channel_name=channel_name,
                user_id=user_id,
                chat_id=chat_id,
                text=user_text,
                session_key=current_session_key,
            )
            try:
                messages = await bridge.handle_inbound(inbound)
            except TimeoutError:
                console.print_text(
                    _format_gateway_timeout_notice(
                        config.manager.request_timeout_seconds
                    )
                )
                continue
            for message in messages:
                ref = tracker.add(
                    text=message.text,
                    session_key=message.session_key or current_session_key,
                    conversation_id=message.conversation_id,
                )
                console.print_message(
                    ref_id=ref.ref_id,
                    conversation_id=message.conversation_id,
                    text=message.text,
                )


async def _run_gateway(config_path: str, *, env_path: str | None = None) -> None:
    await _run_gateway_services(
        config_path, env_path=env_path, include_apps=None, disable_apps=False
    )


async def _run_app(
    config_path: str, *, app_name: str, env_path: str | None = None
) -> None:
    await _run_gateway_services(
        config_path, env_path=env_path, include_apps=[app_name], disable_apps=False
    )


def _interactive_app_list(
    config_path: str,
    *,
    env_path: str | None = None,
) -> None:
    config = _load_gateway_instance_config(config_path, env_path=env_path)
    registry = build_interactive_app_registry(config)
    payload = [
        entry.as_dict()
        for entry in build_interactive_app_catalog(config.interactive_apps, registry)
    ]
    print(json.dumps(payload, indent=2, sort_keys=True))


def _manager_transition_notices_enabled(config: GatewayInstanceConfig) -> bool:
    return bool(config.assistant.manager_transition_notices_enabled)


async def _run_gateway_services(
    config_path: str,
    *,
    env_path: str | None,
    include_apps: list[str] | None,
    disable_apps: bool,
) -> None:
    config = _load_gateway_instance_config(config_path, env_path=env_path)
    _use_gateway_broker_token()
    startup_notices = _consume_headless_startup_notices()
    manager_transition_notices_enabled = _manager_transition_notices_enabled(config)

    async def output_sink(text: str) -> None:
        print(f"\n{text}\n")

    gateway = UserGateway(config, output_sink=output_sink)
    app_manager = InteractiveAppManager(
        config,
        gateway.application,
        output_sink=output_sink,
        include_apps=include_apps,
    )
    registry = app_manager.registry
    adapter_host = (
        GatewayAdapterHostRuntime(
            config,
            gateway.application,
            gateway.state_store.storage_settings,
            registry=registry,
        )
        if config.interactive_apps.adapter_host.enabled and not disable_apps
        else None
    )
    try:
        await gateway.connect()
        if adapter_host is not None:
            await adapter_host.start()
        if not disable_apps and app_manager.has_enabled_apps:
            await app_manager.start()
        has_external_apps = bool(adapter_host is not None and adapter_host.has_routes)
        if not disable_apps and (app_manager.has_running_apps or has_external_apps):

            async def route_gateway_event(event: GatewayDeliveryEvent) -> None:
                if adapter_host is not None and await adapter_host.handle_gateway_event(
                    event
                ):
                    return
                await app_manager.handle_gateway_event(event)

            gateway.bind_event_sink(route_gateway_event)
            if app_manager.has_running_apps:
                gateway.manager_transitions.set_hooks(
                    on_manager_unavailable=app_manager.handle_manager_unavailable,
                    on_manager_available=app_manager.handle_manager_available,
                )
                if config.manager.manager_agent_id is None:
                    await app_manager.handle_manager_unavailable()
                if manager_transition_notices_enabled:
                    gateway.manager_transitions.enable_notices()
            elif manager_transition_notices_enabled:
                gateway.manager_transitions.enable_notices()
            running_names = [
                *app_manager.running_app_names,
                *(adapter_host.channel_names if adapter_host is not None else ()),
            ]
            print(
                "Gateway services started with interactive apps: "
                + ", ".join(running_names)
                + ". Press Ctrl+C to stop."
            )
        else:
            if manager_transition_notices_enabled:
                gateway.manager_transitions.enable_notices()
            if not disable_apps and app_manager.has_enabled_apps:
                print(
                    "Gateway services started, but no configured interactive apps started successfully. "
                    "Using local output only. Press Ctrl+C to stop."
                )
            else:
                print("Gateway services started. Press Ctrl+C to stop.")
        for startup_notice in startup_notices:
            await gateway.proactive_delivery.emit_notice(startup_notice)
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            logger.info("Gateway services interrupted; shutting down")
    finally:
        await app_manager.stop()
        if adapter_host is not None:
            await adapter_host.stop()
        await gateway.close()


async def _run_adapter_host_service(
    config_path: str,
    *,
    env_path: str | None = None,
) -> None:
    config = _load_gateway_instance_config(config_path, env_path=env_path)
    if not config.interactive_apps.adapter_host.enabled:
        raise SystemExit(
            "interactive_apps.adapter_host.enabled must be true to run the adapter host"
        )
    built = build_python_adapter_host_runtime(config)
    await built.runtime.start()
    logger.info(
        "External adapter host started. event=adapter_host_started plugins=%s",
        ",".join(built.plugin_ids),
    )
    try:
        await asyncio.Event().wait()
    except asyncio.CancelledError:
        logger.info("Adapter host interrupted; shutting down")
    finally:
        await built.runtime.stop()


async def _run_activity_collector_service(
    config_path: str, *, env_path: str | None = None
) -> None:
    config = _load_gateway_instance_config(config_path, env_path=env_path)
    await run_activity_collector(build_collector_runtime_settings(config))


async def _run_secure_tool_broker_service(
    config_path: str,
    *,
    env_path: str | None = None,
    broker_env_file: str | None = None,
    host: str | None = None,
    port: int | None = None,
) -> None:
    original_broker_state_env = {
        key: os.environ.get(key) for key in BROKER_CREDENTIAL_STATE_ENV_KEYS
    }
    try:
        config = _load_gateway_instance_config(config_path, env_path=env_path)
        os.environ[BROKER_APPEARANCE_ENV] = json.dumps(
            config.ui.onboarding.appearance.model_dump(mode="json"),
            sort_keys=True,
        )
        broker_env_source = broker_env_file or os.getenv("SECURE_TOOL_BROKER_ENV_FILE")
        if broker_env_source:
            _load_cli_env_file(broker_env_source)
        configure_broker_credential_environment(
            os.getenv("SECURE_TOOL_BROKER_CREDENTIALS_DIR")
            or config.broker_credentials_path,
            credential_state_backend=config.security_layer.credential_state_backend.model_dump(
                mode="json"
            ),
        )
        if config.security_layer.sealbox.enabled:
            broker_credentials_dir = (
                os.getenv("SECURE_TOOL_BROKER_CREDENTIALS_DIR")
                or config.broker_credentials_path
            )
            sealbox_runtime = resolve_sealbox_runtime_config(
                config.security_layer.sealbox,
                broker_credentials_dir=broker_credentials_dir,
                env=os.environ,
            )
            apply_sealbox_cli_env(sealbox_runtime, env=os.environ)
        if config.security_layer.bitwarden_server_url:
            os.environ.setdefault(
                "BITWARDEN_SERVER_URL", config.security_layer.bitwarden_server_url
            )
        if config.security_layer.credential_catalog.enabled:
            os.environ.setdefault(
                "SECURE_TOOL_BROKER_CREDENTIAL_CATALOG_DB_PATH",
                str(config.credential_catalog_database_path),
            )
            os.environ.setdefault(
                "SECURE_TOOL_BROKER_CREDENTIAL_TEMPLATE_OVERRIDES_PATH",
                str(config.credential_template_overrides_path),
            )
            os.environ.setdefault(
                "SECURE_TOOL_BROKER_DEFAULT_CREDENTIAL_WRITE_SOURCE",
                config.security_layer.credential_catalog.default_write_source,
            )
            os.environ.setdefault(
                "SECURE_TOOL_BROKER_OPENSHELL_BINDING_TTL_SECONDS",
                str(
                    config.security_layer.credential_catalog.openshell_binding_ttl_seconds
                ),
            )
            os.environ.setdefault(
                "SECURE_TOOL_BROKER_COLLECTION_SESSION_TTL_SECONDS",
                str(
                    config.security_layer.credential_catalog.collection_session_ttl_seconds
                ),
            )
            os.environ.setdefault(
                "SECURE_TOOL_BROKER_SECRET_CACHE_TTL_SECONDS",
                str(config.security_layer.credential_catalog.secret_cache_ttl_seconds),
            )
        materialize_secret_file_envs()
        internal_base_url = resolve_secure_tool_broker_internal_base_url(
            config.security_layer.broker_internal_base_url
        )
        default_host, default_port = resolve_bind_from_base_url(internal_base_url)
        token = os.getenv("SECURE_TOOL_BROKER_TOKEN", "").strip()
        if not token:
            raise SystemExit(
                "SECURE_TOOL_BROKER_TOKEN is required to run the secure tool broker"
            )
        explicit_security_fields = getattr(
            config.security_layer, "model_fields_set", set()
        )
        config_bind_host = (
            config.security_layer.broker_bind_host
            if "broker_bind_host" in explicit_security_fields
            else None
        )
        config_bind_port = (
            config.security_layer.broker_bind_port
            if "broker_bind_port" in explicit_security_fields
            else None
        )
        resolved_host = (
            host
            or os.getenv("SECURE_TOOL_BROKER_HOST")
            or config_bind_host
            or default_host
        )
        resolved_port = int(
            port
            if port is not None
            else (
                os.getenv("SECURE_TOOL_BROKER_PORT") or config_bind_port or default_port
            )
        )
        if resolved_port < 1 or resolved_port > 65_535:
            raise SystemExit("Secure tool broker port must be between 1 and 65535")
        mcp_config = BrokerMCPConfig.from_runtime_settings(
            instance_root_path=str(config.instance_root_path),
            agent=config.agent,
            agent_types=config.agent_types,
            mcp_profiles=config.mcp_profiles,
            mcp=config.mcp,
        )
        from runtime_settings.services import web_service_urls
        from secure_tool_broker.access_gate import build_broker_access_gate

        access_gate = build_broker_access_gate(
            instance_root=config.instance_root_path,
            team_id=config.manager.team_id,
            authorization_enabled=config.authorization.enabled,
            storage_settings=config.manager_storage_settings,
            cedar_binary=config.authorization.cedar_binary_path,
            lead_agent_type=config.manager.named_agent_type,
            # The Crawl4AI web tool (http:crawl4ai) matches this exact address.
            service_urls=web_service_urls(config.web_services),
        )
        await run_secure_tool_broker(
            host=resolved_host,
            port=resolved_port,
            token=token,
            access_gate=access_gate,
            mcp_config=mcp_config,
            tool_setup_recheck_days=config.security_layer.tool_setup_recheck_days,
        )
    finally:
        for key, original_value in original_broker_state_env.items():
            if original_value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = original_value


async def _cron_add(args: argparse.Namespace) -> None:
    config = _load_gateway_instance_config(
        args.config, env_path=getattr(args, "env", None)
    )
    async with UserGateway(config, output_sink=None) as gateway:
        job_id = await gateway.application.add_cron_job(
            session_key=args.session_key,
            name=args.name,
            instruction=args.instruction,
            interval_seconds=args.interval_seconds,
            cron_expression=args.cron_expression,
        )
    print(job_id)


async def _cron_list(config_path: str, *, env_path: str | None = None) -> None:
    config = _load_gateway_instance_config(config_path, env_path=env_path)
    async with UserGateway(config, output_sink=None) as gateway:
        jobs = await gateway.application.list_scheduled_jobs()
    for job in jobs:
        name = job.schedule_name or job.scheduled_job_id
        print(
            f"{job.scheduled_job_id} | enabled={job.enabled} | next={job.next_fire_at} | "
            f"paused_until={job.paused_until or '-'} | session={job.session_key} | name={name}"
        )


async def _cron_remove(
    config_path: str, job_id: str, *, env_path: str | None = None
) -> None:
    config = _load_gateway_instance_config(config_path, env_path=env_path)
    async with UserGateway(config, output_sink=None) as gateway:
        removed = await gateway.application.remove_scheduled_job(job_id)
    if not removed:
        raise SystemExit(f"Job not found: {job_id}")
    print(job_id)


def _resolve_pause_until(*, minutes: Optional[int], until_iso: Optional[str]) -> str:
    if minutes is None and not until_iso:
        raise SystemExit("Provide --minutes or --until-iso.")
    if minutes is not None and until_iso:
        raise SystemExit("Provide only one of --minutes or --until-iso.")
    if until_iso:
        dt = datetime.fromisoformat(until_iso)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc).isoformat()
    return (datetime.now(timezone.utc) + timedelta(minutes=int(minutes))).isoformat()


async def _cron_pause(
    config_path: str,
    job_id: str,
    *,
    minutes: Optional[int],
    until_iso: Optional[str],
    env_path: str | None = None,
) -> None:
    config = _load_gateway_instance_config(config_path, env_path=env_path)
    paused_until = _resolve_pause_until(minutes=minutes, until_iso=until_iso)
    async with UserGateway(config, output_sink=None) as gateway:
        paused = await gateway.application.pause_scheduled_job(
            job_id, paused_until=paused_until
        )
    if not paused:
        raise SystemExit(f"Job not found: {job_id}")
    print(f"{job_id} paused until {paused_until}")


async def _cron_resume(
    config_path: str, job_id: str, *, env_path: str | None = None
) -> None:
    config = _load_gateway_instance_config(config_path, env_path=env_path)
    async with UserGateway(config, output_sink=None) as gateway:
        resumed = await gateway.application.resume_scheduled_job(job_id)
    if not resumed:
        raise SystemExit(f"Job not found: {job_id}")
    print(job_id)


async def _cron_disable(
    config_path: str, job_id: str, *, env_path: str | None = None
) -> None:
    config = _load_gateway_instance_config(config_path, env_path=env_path)
    async with UserGateway(config, output_sink=None) as gateway:
        disabled = await gateway.application.disable_scheduled_job(job_id)
    if not disabled:
        raise SystemExit(f"Job not found: {job_id}")
    print(job_id)


def _execution_cli_repository(config: GatewayInstanceConfig) -> Any:
    from storage.execution_control_repository import create_execution_control_repository

    return create_execution_control_repository(
        config.resolve_manager_storage_settings(env=os.environ),
        logger=logger,
    )


async def _run_execution_controller_service(
    config_path: str,
    *,
    env_path: str | None = None,
) -> None:
    from runtime_hosting.controller_service import run_execution_controller

    config = _load_gateway_instance_config(config_path, env_path=env_path)
    await run_execution_controller(config, logger=logger)


def _execution_job_list(args: argparse.Namespace) -> None:
    config = _load_gateway_instance_config(
        args.config,
        env_path=getattr(args, "env", None),
    )
    repository = _execution_cli_repository(config)
    jobs = repository.list_jobs(team_id=config.gateway.team_id)
    if not args.include_disabled:
        jobs = tuple(job for job in jobs if job.enabled)
    payloads = [job.model_dump(mode="json") for job in jobs]
    if args.json:
        print(json.dumps({"returned": len(payloads), "jobs": payloads}, indent=2, sort_keys=True))
        return
    for job in jobs:
        print(
            f"{job.job_id} | enabled={job.enabled} | next={job.next_run_at_utc or '-'} | "
            f"revision={job.revision_id} | generation={job.generation} | name={job.name}"
        )


def _execution_job_show(args: argparse.Namespace) -> None:
    config = _load_gateway_instance_config(
        args.config,
        env_path=getattr(args, "env", None),
    )
    repository = _execution_cli_repository(config)
    job = repository.get_job(team_id=config.gateway.team_id, job_id=args.job_id)
    if job is None:
        raise SystemExit(f"Execution job not found in this team scope: {args.job_id}")
    attempts = repository.list_run_attempts(
        team_id=config.gateway.team_id,
        job_id=job.job_id,
        limit=20,
    )
    print(
        json.dumps(
            {
                "job": job.model_dump(mode="json"),
                "attempts": [attempt.model_dump(mode="json") for attempt in attempts],
            },
            indent=2,
            sort_keys=True,
        )
    )


def _execution_retire_old_records(args: argparse.Namespace) -> None:
    """Retire execution records saved before access model 2 (explicit operator step).

    Their activations name decisions of the removed permission store, so the
    execution controller refuses to start while any remain. Retiring keeps the
    records but drops that authority: jobs are disabled, services are set to
    stopped and SSH sessions can no longer be opened.
    """

    config = _load_gateway_instance_config(
        args.config,
        env_path=getattr(args, "env", None),
    )
    repository = _execution_cli_repository(config)
    retired = repository.retire_old_format_records(team_id=config.gateway.team_id)
    if not retired.present:
        print("No execution records from before access model 2 were found.")
        return
    for table, record_id in retired.records:
        print(f"Retired {record_id} ({table})")
    print(
        "Start Universal Agents again. Re-enable jobs with set_execution_job_enabled, and start "
        "services again with plan_managed_execution_service_state and apply_change."
    )


def _objective_list(args: argparse.Namespace) -> None:
    config, repository = _load_objective_repository(
        args.config, env_path=getattr(args, "env", None)
    )
    owner_agent_id = _resolve_objective_owner_agent_id(
        config, getattr(args, "owner_agent_id", None)
    )
    statuses = [
        value.strip() for value in (args.status or "").split(",") if value.strip()
    ] or None
    rows = repository.list_objectives(
        limit=args.limit,
        owner_agent_id=owner_agent_id,
        statuses=statuses,
        include_closed=bool(args.include_closed),
    )
    for row in rows:
        print(
            f"{row.objective_id} | status={row.status} | horizon={row.horizon} | "
            f"priority={row.priority} | current_plan={row.current_plan_id or '-'} | {row.title}"
        )


def _objective_show(args: argparse.Namespace) -> None:
    config, repository = _load_objective_repository(
        args.config, env_path=getattr(args, "env", None)
    )
    owner_agent_id = _resolve_objective_owner_agent_id(
        config, getattr(args, "owner_agent_id", None)
    )
    objective = repository.get_objective(
        args.objective_id, owner_agent_id=owner_agent_id
    )
    if objective is None:
        raise SystemExit(f"Objective not found: {args.objective_id}")
    print(objective.model_dump_json(indent=2))


async def _heartbeat_pause(
    config_path: str,
    *,
    minutes: Optional[int],
    until_iso: Optional[str],
    env_path: str | None = None,
) -> None:
    config = _load_gateway_instance_config(config_path, env_path=env_path)
    paused_until = _resolve_pause_until(minutes=minutes, until_iso=until_iso)
    async with UserGateway(config, output_sink=None) as gateway:
        paused = await gateway.application.pause_scheduled_job(
            config.heartbeat.schedule_id,
            paused_until=paused_until,
        )
    if not paused:
        raise SystemExit(
            f"Heartbeat schedule not found: {config.heartbeat.schedule_id}"
        )
    print(f"{config.heartbeat.schedule_id} paused until {paused_until}")


async def _heartbeat_resume(config_path: str, *, env_path: str | None = None) -> None:
    config = _load_gateway_instance_config(config_path, env_path=env_path)
    async with UserGateway(config, output_sink=None) as gateway:
        resumed = await gateway.application.resume_scheduled_job(
            config.heartbeat.schedule_id
        )
    if not resumed:
        raise SystemExit(
            f"Heartbeat schedule not found: {config.heartbeat.schedule_id}"
        )
    print(config.heartbeat.schedule_id)


async def _heartbeat_disable(config_path: str, *, env_path: str | None = None) -> None:
    config = _load_gateway_instance_config(config_path, env_path=env_path)
    async with UserGateway(config, output_sink=None) as gateway:
        disabled = await gateway.application.disable_scheduled_job(
            config.heartbeat.schedule_id
        )
    if not disabled:
        raise SystemExit(
            f"Heartbeat schedule not found: {config.heartbeat.schedule_id}"
        )
    print(config.heartbeat.schedule_id)


async def _deliver_pending(config_path: str, *, env_path: str | None = None) -> None:
    config = _load_gateway_instance_config(config_path, env_path=env_path)

    async def output_sink(text: str) -> None:
        print(text)

    async with UserGateway(
        config, output_sink=output_sink, state_store=None
    ) as gateway:
        await gateway.application.release_due_deliveries()


def _memory_graph_embedding_status(
    config_path: str, *, env_path: str | None = None
) -> None:
    config, repository = _load_memory_graph_repository(config_path, env_path=env_path)
    report = inspect_memory_graph_embedding_profile(
        repository=repository,
        profile_spec=build_embedding_profile_spec(
            _current_embedding_profile_spec(config)
        ),
    )
    print(json.dumps(report, indent=2, sort_keys=True))


def _memory_graph_embedding_reset(
    config_path: str,
    *,
    env_path: str | None = None,
    archive: bool,
    purge: bool,
) -> None:
    _ = purge  # argparse mutual exclusion is already enforced
    _, repository = _load_memory_graph_repository(config_path, env_path=env_path)
    if archive:
        counts_before = repository.count_graph_embedding_rows()
        archived = repository.archive_active_embedding_profile(
            metadata={"row_counts_at_archive": counts_before}
        )
        cleared = repository.purge_graph_embeddings()
        payload = {
            "action": "archive",
            "archived_profile": {
                "embedding_profile_id": archived.embedding_profile_id,
                "provider": archived.provider,
                "model_id": archived.model_id,
                "api_base": archived.api_base,
                "dimensions": archived.dimensions,
                "archived_at": archived.archived_at,
            }
            if archived is not None
            else None,
            "cleared_rows": cleared,
        }
        print(json.dumps(payload, indent=2, sort_keys=True))
        return

    cleared = repository.purge_graph_embeddings()
    repository.purge_embedding_profiles()
    print(
        json.dumps(
            {
                "action": "purge",
                "cleared_rows": cleared,
                "profiles_removed": True,
            },
            indent=2,
            sort_keys=True,
        )
    )


def _access_list(args: argparse.Namespace, config: Any, service: Any) -> None:
    from access_control.operator_commands import describe_grant, grant_matches, list_grants

    grants = [
        grant
        for grant in list_grants(service.store, service.team_id, include_ended=args.all)
        if grant_matches(grant, args.match)
    ]
    for grant in grants:
        print(describe_grant(grant))
    if not grants:
        print("No grants.")


def _access_links(args: argparse.Namespace, config: Any, service: Any) -> None:
    from access_control.operator_commands import describe_link

    links = service.store.list_links(service.team_id, include_revoked=args.all)
    for link in links:
        print(describe_link(link))
    if not links:
        print("No account links.")


def _acting_person(args: argparse.Namespace, config: Any) -> Any:
    """The team member a change is made as (``--by``, default the instance's user)."""

    from access_control.operator_commands import person_for
    from assistant_gateway.access_setup import MembershipRoles

    return person_for(
        MembershipRoles.for_config(config), args.by or config.user.canonical_user_id, config.manager.team_id
    )


def _access_grant(args: argparse.Namespace, config: Any, service: Any) -> None:
    from access_control.operator_commands import GrantRequest, build_grant_spec, describe_grant

    person = _acting_person(args, config)
    spec = build_grant_spec(
        GrantRequest(
            to=args.to,
            tool=args.tool,
            tool_set=args.tool_set,
            folder=args.folder,
            level=args.level,
            duration=args.duration,
            delegate_depth=args.delegate_depth,
            delegate_mode=args.delegate_mode,
            reason=args.reason,
            everyone=args.everyone,
        ),
        team_id=service.team_id,
        by=person.person_id,
        now=service.clock(),
    )
    cover = service.covering_grant(spec)
    if cover is not None:
        print("Already given; nothing changed: " + describe_grant(cover))
        return
    print("Created " + describe_grant(service.grant_by_person(person, spec)))


def _access_revoke(args: argparse.Namespace, config: Any, service: Any) -> None:
    revoked = service.revoke_by_person(_acting_person(args, config), args.grant_id)
    print(f"Revoked {len(revoked)} grant(s): {', '.join(revoked)}")


def _access_starter_profile(args: argparse.Namespace, config: Any, service: Any) -> None:
    from access_control.operator_commands import describe_grant
    from access_control.starter_profiles import apply_starter_profile, starter_profiles

    created = apply_starter_profile(
        service, _acting_person(args, config), args.profile, lead_agent_type=config.manager.named_agent_type
    )
    profile = starter_profiles()[args.profile]
    print(f"{profile.display_name}: {profile.description}")
    for grant in created:
        print("  " + describe_grant(grant))


def _access_unlink(args: argparse.Namespace, config: Any, service: Any) -> None:
    from access_control.operator_commands import describe_link, unlink

    link = unlink(service.store, _acting_person(args, config), args.link_id, now=service.clock())
    print("Removed " + describe_link(link))


def _access_requests(args: argparse.Namespace, config: Any, service: Any) -> None:
    _access_request_command(args, config, service)


def _access_approve_existing(args: argparse.Namespace, config: Any, service: Any) -> None:
    _approve_existing_command(args, config)


# The ``access`` subcommands that read or change the access store, by name.
_ACCESS_SERVICE_COMMANDS: dict[str, Callable[[argparse.Namespace, Any, Any], None]] = {
    "list": _access_list,
    "links": _access_links,
    "requests": _access_requests,
    "decide": _access_requests,
    "block": _access_requests,
    "unblock": _access_requests,
    "approve-existing": _access_approve_existing,
    "grant": _access_grant,
    "revoke": _access_revoke,
    "starter-profile": _access_starter_profile,
    "unlink": _access_unlink,
}


def _access_command(args: argparse.Namespace) -> None:
    """Give, revoke or list grants and the other ``access`` subcommands (access_control.operator_commands)."""

    from access_control.decisions import HandDownRefused
    from access_control.operator_commands import OperatorCommandError, open_access_service

    config = _load_gateway_instance_config(args.config)
    if args.access_command == "drop-old-tables":
        _drop_old_access_tables_command(args, config)
        return
    if args.access_command == "folders":
        _data_folders_command(args, config)
        return
    service = open_access_service(
        instance_root=config.instance_root_path,
        team_id=config.manager.team_id,
        storage_settings=config.manager_storage_settings,
        cedar_binary=config.authorization.cedar_binary_path,
        lead_agent_type=config.manager.named_agent_type,
    )
    if not config.authorization.enabled:
        print("Authorization is off, so grants are recorded but not enforced until it is turned on.")
    try:
        handler = _ACCESS_SERVICE_COMMANDS.get(args.access_command)
        if handler is not None:
            handler(args, config, service)
    except (OperatorCommandError, HandDownRefused, ValueError) as exc:
        raise SystemExit(str(exc)) from exc
    finally:
        service.cedar.close()


def _data_folders_command(args: argparse.Namespace, config: Any) -> None:
    """``access folders list|add|remove``: the team's data folders (decision D16).

    Works in both authorization modes: with it off, every agent uses the
    registered folders at their writability.
    """

    from access_control.data_folders import DataFolderRefused, DataFolderRegistry, protected_locations
    from access_control.operator_commands import OperatorCommandError, person_for
    from assistant_gateway.access_setup import MembershipRoles
    from storage.access_repository import AccessRepository

    team_id = config.manager.team_id
    registry = DataFolderRegistry(AccessRepository(config.manager_storage_settings), team_id)
    now = datetime.now(timezone.utc)
    try:
        if args.folder_action == "list":
            records = registry.records()
            for record in records:
                access = "read and change" if record.writable else "read only"
                print(f"data/{record.name}: {record.path} ({access}); registered by {record.registered_by.id}")
            if not records:
                print("No data folders.")
            return
        if not args.name:
            raise SystemExit(f"access folders {args.folder_action} needs the folder's name.")
        person = person_for(MembershipRoles.for_config(config), args.by or config.user.canonical_user_id, team_id)
        if args.folder_action == "remove":
            removed = registry.remove(person, args.name, now=now)
            print(f"Removed data/{removed.name} ({removed.path}).")
            return
        if not args.path:
            raise SystemExit("access folders add needs the folder's absolute path.")
        record = registry.register(
            person,
            name=args.name,
            path=args.path,
            writable=not args.read_only,
            reason=args.reason,
            protected=protected_locations(
                config.instance_root_path,
                instance_config=Path(args.config).expanduser().resolve(),
                broker_credentials=getattr(config, "broker_credentials_path", None),
            ),
            now=now,
        )
        print(f"Registered data/{record.name}: {record.path} ({'read and change' if record.writable else 'read only'}).")
    except (DataFolderRefused, OperatorCommandError) as exc:
        raise SystemExit(str(exc)) from exc


def _drop_old_access_tables_command(args: argparse.Namespace, config: Any) -> None:
    """List, and with --yes drop, the tables the removed permission system left behind.

    Upgrading keeps them untouched so they can be inspected first; no code reads
    them (storage.old_access_tables names each one and where it lived). Dropping
    cannot be undone, so it runs only with --yes.
    """

    from storage.old_access_tables import (
        OLD_CREDENTIAL_CATALOG_TABLES,
        OLD_STATE_TABLES,
        OldTableDatabase,
        drop_old_tables,
        find_old_tables,
        merge_databases,
        sqlite_database,
    )

    databases = merge_databases(
        [
            OldTableDatabase("manager database", config.manager_storage_settings, OLD_STATE_TABLES),
            OldTableDatabase("gateway database", config.state_storage_settings, OLD_STATE_TABLES),
            sqlite_database(
                "credential catalog", config.credential_catalog_database_path, OLD_CREDENTIAL_CATALOG_TABLES
            ),
        ]
    )
    found_any = False
    for database in databases:
        found = find_old_tables(database)
        if not found:
            print(f"{database.label} ({database.location}): no old tables.")
            continue
        found_any = True
        print(f"{database.label} ({database.location}):")
        for table in found:
            print(f"  {table.name}: {table.rows} row(s)")
        if args.yes:
            dropped = drop_old_tables(database, found)
            print(f"  Dropped {len(dropped)} table(s).")
    if found_any and not args.yes:
        print("Nothing was changed. Run again with --yes to drop these tables; this cannot be undone.")


def _access_request_command(args: argparse.Namespace, config: Any, service: Any) -> None:
    """List or decide agents' access requests, or block and unblock (access_control.requests).

    A decision made here is sent to the waiting agent by the gateway's next
    Inbox pass, like one the gateway could not send at once. A block applies
    from the next access decision.
    """

    from access_control.inbox import StoredAccessInbox
    from access_control.operator_commands import (
        block_subject,
        block_target,
        describe_grant,
        describe_request,
        person_for,
    )
    from access_control.requests import AccessRequests
    from assistant_gateway.access_setup import MembershipRoles
    from custom_messages.contracts.access_v2 import AccessRequestStatus, AccessReviewChoice

    team_id = config.manager.team_id
    requests = AccessRequests(
        service=service, inbox=StoredAccessInbox(service.store), lead_agent_type=config.manager.named_agent_type
    )
    if args.access_command == "requests":
        found = service.store.list_requests(team_id, status=None if args.all else AccessRequestStatus.WAITING)
        for request in found:
            print(describe_request(requests, request))
        if not found:
            print("No access requests are waiting." if not args.all else "No access requests.")
        return
    person = person_for(MembershipRoles.for_config(config), args.by or config.user.canonical_user_id, team_id)
    if args.access_command == "block":
        block = requests.block(
            person,
            subject=block_subject(
                agent_type=args.agent_type,
                agent=args.agent,
                person=args.person,
                team_id=team_id,
                role=args.role,
                team=args.team,
            ),
            target=block_target(tool=args.tool, operation=args.operation, folder=args.folder),
            reason=args.reason,
            hours=args.hours,
            replace_block_id=args.replace_block_id,
        )
        action = f"Replaced block {args.replace_block_id}: " if args.replace_block_id else "Blocked "
        print(action + describe_grant(block))
        print("It applies from the next access decision.")
        return
    if args.access_command == "unblock":
        print("Removed " + describe_grant(requests.unblock(person, args.grant_id)))
        return
    decided = requests.decide(args.request_id, person, AccessReviewChoice(args.choice), note=args.note)
    print("Decided " + describe_request(requests, decided))
    print("The waiting agent is told within a few seconds while the gateway runs.")


def _approve_existing_command(args: argparse.Namespace, config: Any) -> None:
    """Re-approve old-format records as an administrator and rewrite old scheduled-worker settings.

    Covers MCP registry records (files) and saved scheduled-worker settings
    (manager database). Startup refuses
    while any exist (runtime_settings.approval_cutover); this is the explicit,
    person-run step that records who approved them now.
    """

    from datetime import datetime, timezone

    from access_control.operator_commands import person_for
    from access_control.people import is_administrator
    from assistant_gateway.access_setup import MembershipRoles
    from mcp_control.registry import MCPConnectionRecordV1
    from runtime_settings.approval_cutover import old_approval_records, reapprove

    team_id = config.manager.team_id
    person = person_for(MembershipRoles.for_config(config), args.by or config.user.canonical_user_id, team_id)
    if not is_administrator(person.roles):
        raise SystemExit("Only a team administrator or owner can re-approve existing configuration.")
    found = old_approval_records(
        config.instance_root_path,
        storage_settings=config.manager_storage_settings,
    )
    if not found.present:
        print("Nothing to re-approve: every saved approval is in the current format.")
        return
    for line in reapprove(found, person_id=person.person_id, now=datetime.now(timezone.utc)):
        print(f"Re-approved {line}")
    # Prove the rewritten files load with the current models before anyone starts the system.
    for path in found.mcp_connections:
        MCPConnectionRecordV1.model_validate_json(path.read_text(encoding="utf-8"))
    if found.schedule_worker_bindings:
        from storage.schedule_worker_repository import old_schedule_worker_bindings

        remaining = old_schedule_worker_bindings(config.manager_storage_settings)
        if remaining:
            raise SystemExit(f"Some scheduled-worker settings are still in the old format: {', '.join(remaining)}")
    print(f"Recorded {person.person_id} as the approver. Start Universal Agents again.")


def _mcp_operation_levels(args: argparse.Namespace) -> None:
    """Show hints and levels of installed MCP operations, or confirm levels.

    MCP annotations are untrusted hints; an administrator's confirmation is
    what lets a read or write grant cover an operation
    (mcp_control.operation_levels). Confirming is optional: a grant at full on
    the server's tool (``mcp:<name>``) covers every operation, confirmed or
    not, so levels matter only to people who want to give read-only use.
    """

    from mcp_control.operation_levels import confirm_operation_levels
    from mcp_control.registry import (
        MCPConnectionLifecycle,
        MCPConnectionStore,
        resolve_mcp_connection_paths,
    )

    config = GatewayInstanceConfig.load(args.config)
    store = MCPConnectionStore(resolve_mcp_connection_paths(config.instance_root_path))
    records = [
        record
        for record in store.list_records()
        if record.lifecycle is MCPConnectionLifecycle.ACTIVE
        and (args.connection is None or record.descriptor.connection_name == args.connection)
    ]
    if args.connection and not records:
        raise SystemExit(f"No installed MCP connection is named {args.connection!r}.")
    changes = _operation_level_changes(args, store, records)
    if changes:
        # Confirming a lower level lets narrower grants cover the operation,
        # so only an active administrator may record it (access plan 7d).
        confirmed_by = _confirming_administrator(args, config)
        try:
            confirm_operation_levels(store.paths, records[0].connection_id, changes, confirmed_by=confirmed_by)
        except KeyError as exc:
            raise SystemExit(str(exc)) from exc
        print(f"Confirmed {len(changes)} operation level(s) for {records[0].descriptor.connection_name}.")
    _print_operation_levels(store, records)


def _operation_level_changes(args: argparse.Namespace, store: Any, records: list[Any]) -> dict[str, Any]:
    """The levels ``--set`` and ``--accept-suggested`` ask to confirm."""

    from custom_messages.contracts.access_v2 import AccessLevel
    from mcp_control.operation_levels import load_operation_levels

    changes: dict[str, AccessLevel] = {}
    for item in args.set:
        name, _, level = str(item).partition("=")
        try:
            changes[name.strip()] = AccessLevel(level.strip().lower())
        except ValueError as exc:
            raise SystemExit(f"--set expects OPERATION=read|write|full, not {item!r}") from exc
    if (changes or args.accept_suggested) and len(records) != 1:
        raise SystemExit("Name one connection with --connection to confirm levels.")
    if records and args.accept_suggested:
        current = load_operation_levels(store.paths, records[0].connection_id)
        for name, entry in current.operations.items():
            if entry.confirmed_level is None:
                changes.setdefault(name, entry.suggested_level)
    return changes


def _confirming_administrator(args: argparse.Namespace, config: Any) -> str:
    """The person id of ``--by`` (default: the instance's user); refuses anyone but an active administrator."""

    from access_control.operator_commands import OperatorCommandError, person_for
    from access_control.people import is_administrator
    from assistant_gateway.access_setup import MembershipRoles

    try:
        person = person_for(
            MembershipRoles.for_config(config), args.by or config.user.canonical_user_id, config.manager.team_id
        )
    except OperatorCommandError as exc:
        raise SystemExit(str(exc)) from exc
    if not is_administrator(person.roles):
        raise SystemExit("Only a team administrator or owner can confirm MCP operation levels.")
    return person.person_id


def _print_operation_levels(store: Any, records: list[Any]) -> None:
    from mcp_control.operation_levels import load_operation_levels

    for record in records:
        levels = load_operation_levels(store.paths, record.connection_id).operations
        print(f"{record.descriptor.connection_name} ({record.connection_id})")
        for operation in record.operations:
            entry = levels.get(operation.operation_name)
            confirmed = (
                entry.confirmed_level.value
                if entry and entry.confirmed_level and entry.operation_digest == operation.operation_digest
                else "not confirmed (needs full)"
            )
            suggested = entry.suggested_level.value if entry else "full"
            hints = ", ".join(f"{key}={value}" for key, value in (entry.hints if entry else {}).items()) or "none"
            print(f"  {operation.operation_name}: {confirmed}; suggested {suggested}; hints {hints}")


def _migrate_config(config_path: str, *, assume_yes: bool) -> None:
    status = inspect_instance_config_version(config_path)
    print(f"Config: {status.path}")
    print(f"Detected schema_version: {status.detected_version}")
    print(f"Latest schema_version: {status.latest_version}")
    print(
        "Directly compatible schema versions: "
        + ", ".join(str(version) for version in status.directly_compatible_versions)
    )
    if not status.needs_migration:
        print("No migration required.")
        return

    if not assume_yes:
        if not sys.stdin.isatty() or not sys.stdout.isatty():
            raise SystemExit(
                "Migration required. Re-run with --yes to apply it non-interactively."
            )
        raw = input("Apply migration now? [Y/n]: ").strip().lower()
        if raw not in {"", "y", "yes"}:
            raise SystemExit("Migration declined.")

    result = migrate_instance_config_file(status.path)
    print(f"Migrated to schema_version {result.migrated_version}")
    if result.backup_path is not None:
        print(f"Backup written to {result.backup_path}")
    for step in result.applied_steps:
        print(f"- {step}")


def _onboard(args: argparse.Namespace) -> None:
    _load_cli_env_file(getattr(args, "env", None))
    reject_removed_public_url_env_vars()
    seed = _resolve_onboarding_seed(args)
    answers = _collect_onboarding_answers(args, template=seed.template)
    desired_config = GatewayInstanceConfig.create(
        instance_name=str(answers["instance_name"]),
        instance_root=answers["instance_root"],
        team_id=str(answers["team_id"]),
        timezone=str(answers["timezone"]),
        response_queue_name=args.response_queue_name,
        manager_agent_id=args.manager_agent_id,
        request_timeout_seconds=float(answers["request_timeout_seconds"]),
        manager_storage_backend=seed.template.manager_storage_backend,
        manager_storage_sqlite_path=seed.template.manager_storage_sqlite_path,
        manager_storage_postgres_dsn=seed.template.manager_storage_postgres_dsn,
        heartbeat_enabled=bool(answers["heartbeat_enabled"]),
        heartbeat_interval_seconds=int(answers["heartbeat_interval_seconds"]),
        quiet_hours_enabled=bool(answers["quiet_enabled"]),
        quiet_start_hour_local=int(answers["quiet_start"]),
        quiet_end_hour_local=int(answers["quiet_end"]),
        digest_delivery_hour_local=int(answers["digest_hour"]),
        display_name=str(answers["user_display_name"]),
        assistant_name=str(answers["assistant_name"]),
        initial_user_context=str(answers.get("user_context") or ""),
    )
    config = desired_config
    # The base config's settings first, through the same function the web
    # onboarding uses, then the answers below on top of them.
    apply_onboarding_template(config, seed.template)
    config.user.role = str(answers["user_role"])
    config.user.initial_context = str(answers.get("user_context") or "").strip()
    config.delivery.quiet_hours.enabled = bool(answers["quiet_enabled"])
    config.delivery.quiet_hours.start_hour_local = int(answers["quiet_start"])
    config.delivery.quiet_hours.end_hour_local = int(answers["quiet_end"])
    config.delivery.digest_delivery_hour_local = int(answers["digest_hour"])
    config.delivery.default_target.channel_name = str(
        answers["default_delivery_channel"]
    )
    config.delivery.default_target.chat_id = str(answers["default_delivery_chat_id"])
    config.delivery.default_target.thread_id = str(
        answers["default_delivery_thread_id"]
    )
    config.heartbeat.active_hours_enabled = bool(
        answers["heartbeat_active_hours_enabled"]
    )
    config.heartbeat.active_start_hour_local = int(answers["heartbeat_active_start"])
    config.heartbeat.active_end_hour_local = int(answers["heartbeat_active_end"])
    config.llm.provider = str(answers["model_provider"])
    config.llm.model_id = str(answers["model_id"])
    config.llm.reasoning_effort = str(answers["reasoning_effort"] or "").strip() or None
    config.llm.reasoning_summary = normalize_reasoning_summary(
        answers.get("reasoning_summary")
    )
    config.llm.litellm_api_mode = normalize_litellm_api_mode(
        answers.get("litellm_api_mode")
    )
    config.security_layer.mode = normalize_security_layer_mode(
        answers["security_layer_mode"]
    )
    config.security_layer.broker_internal_base_url = (
        str(answers["secure_tool_broker_internal_base_url"] or "").strip() or None
    )
    config.network.public_origin = str(
        answers["public_origin"] or NETWORK_DEFAULTS.public_origin.value
    ).strip()
    config.security_layer.broker_public_port = (
        int(answers["secure_tool_broker_public_port"])
        if answers["secure_tool_broker_public_port"] is not None
        else None
    )
    config.security_layer.broker_bind_host = (
        str(answers["secure_tool_broker_bind_host"] or "").strip() or None
    )
    config.security_layer.broker_bind_port = answers["secure_tool_broker_bind_port"]
    config.security_layer.broker_credentials_dir = (
        str(answers["secure_tool_broker_credentials_dir"] or "").strip() or None
    )
    config.interactive_apps.internal_website.bind_host = str(
        answers["internal_website_bind_host"] or ""
    ).strip()
    config.interactive_apps.internal_website.bind_port = int(
        answers["internal_website_bind_port"]
    )
    config.interactive_apps.internal_website.public_port = (
        int(answers["internal_website_public_port"])
        if answers["internal_website_public_port"] is not None
        else None
    )
    config.interactive_apps.internal_website.docs_root = (
        str(answers["internal_website_docs_root"] or "").strip() or None
    )
    config.interactive_apps.internal_website.skills_dir = (
        str(answers["internal_website_skills_dir"] or "").strip() or None
    )
    config.interactive_apps.discord.enabled = bool(answers["discord_enabled"])
    config.interactive_apps.discord.allowed_user_ids = list(
        answers["discord_allowed_user_ids"]
    )
    config.interactive_apps.discord.allowed_channel_ids = list(
        answers["discord_allowed_channel_ids"]
    )
    config.interactive_apps.discord.group_policy = str(answers["discord_group_policy"])
    config.interactive_apps.discord.thread_scoped_sessions = bool(
        answers["discord_thread_scoped_sessions"]
    )
    config.interactive_apps.discord.command_guild_ids = list(
        answers["discord_command_guild_ids"]
    )
    config.interactive_apps.telegram.enabled = bool(answers["telegram_enabled"])
    config.interactive_apps.telegram.allowed_user_ids = list(
        answers["telegram_allowed_user_ids"]
    )
    config.interactive_apps.telegram.allowed_chat_ids = list(
        answers["telegram_allowed_chat_ids"]
    )
    config.interactive_apps.telegram.group_policy = str(
        answers["telegram_group_policy"]
    )
    config.interactive_apps.telegram.manager_offline_policy = str(
        answers["telegram_manager_offline_policy"]
    )
    config.interactive_apps.telegram.topic_scoped_sessions = bool(
        answers["telegram_topic_scoped_sessions"]
    )
    config.interactive_apps.telegram.drop_pending_updates = bool(
        answers["telegram_drop_pending_updates"]
    )
    config.interactive_apps.signal.enabled = bool(answers["signal_enabled"])
    config.interactive_apps.signal.base_url = str(answers["signal_base_url"])
    config.interactive_apps.signal.account = str(answers["signal_account"])
    config.interactive_apps.signal.allowed_sender_ids = list(
        answers["signal_allowed_sender_ids"]
    )
    config.interactive_apps.signal.allowed_group_ids = list(
        answers["signal_allowed_group_ids"]
    )
    config.interactive_apps.signal.group_policy = str(answers["signal_group_policy"])
    config.interactive_apps.signal.mention_markers = list(
        answers["signal_mention_markers"]
    )
    config.interactive_apps.signal.poll_timeout_seconds = float(
        answers["signal_poll_timeout_seconds"]
    )
    config.interactive_apps.signal.max_messages_per_poll = int(
        answers["signal_max_messages_per_poll"]
    )
    config.interactive_apps.signal.drop_pending_messages_on_startup = bool(
        answers["signal_drop_pending_messages_on_startup"]
    )
    config.interactive_apps.photon.enabled = bool(answers["photon_enabled"])
    config.interactive_apps.photon.project_id = str(
        answers["photon_project_id"] or ""
    ).strip()
    config.interactive_apps.photon.allowed_sender_ids = list(
        answers["photon_allowed_sender_ids"]
    )
    config.interactive_apps.photon.allowed_group_ids = list(
        answers["photon_allowed_group_ids"]
    )
    config.interactive_apps.photon.group_policy = str(answers["photon_group_policy"])
    config.interactive_apps.photon.mention_markers = list(
        answers["photon_mention_markers"]
    )
    config.interactive_apps.whatsapp.enabled = bool(answers["whatsapp_enabled"])
    config.interactive_apps.whatsapp.allowed_sender_ids = list(
        answers["whatsapp_allowed_sender_ids"]
    )
    config.interactive_apps.whatsapp.webhook_host = str(
        answers["whatsapp_webhook_host"]
    )
    config.interactive_apps.whatsapp.webhook_port = int(
        answers["whatsapp_webhook_port"]
    )
    config.interactive_apps.whatsapp.webhook_path = str(
        answers["whatsapp_webhook_path"]
    )
    config.interactive_apps.whatsapp.public_base_url = (
        str(answers["whatsapp_public_base_url"]).strip() or None
    )
    config.interactive_apps.whatsapp.validate_twilio_signature = bool(
        answers["whatsapp_validate_twilio_signature"]
    )
    secrets_source = (
        SECRETS_SOURCE_ENVIRONMENT
        if getattr(args, "secrets_from_environment", False)
        else seed.template.secrets_source
    )
    config.launch.secrets_source = secrets_source  # type: ignore[assignment]  # normalized by the template
    config.launch.env_file_path = launch_env_file_for(config.instance_root, secrets_source)
    config = GatewayInstanceConfig.model_validate(config.model_dump(mode="json"))

    config_path = config.config_path
    existing_config_found = config_path.exists()
    before_config = read_yaml_mapping(config_path)
    expected_config_revision = None
    if existing_config_found:
        existing_config = GatewayInstanceConfig.load(config_path)
        # Reconciliation validates a fresh model and drops private attributes.
        # Keep the revision of the destination read before reconciliation so a
        # concurrent update is refused rather than accepted as the new baseline.
        expected_config_revision = existing_config.source_revision
        config = _reconcile_existing_onboarding_config(existing_config, config, args)
        config = GatewayInstanceConfig.model_validate(config.model_dump(mode="json"))

    if config.launch.secrets_from_environment:
        _refuse_env_file_options_for_environment_secrets(args, seed)
        created_paths, before_env, after_env = _write_cli_onboarding_with_environment_secrets(
            config, answers, expected_config_revision=expected_config_revision
        )
    else:
        created_paths, before_env, after_env = _write_cli_onboarding_env_file(
            config, answers, args=args, seed=seed, expected_config_revision=expected_config_revision
        )
    config_added, config_updated, config_removed = diff_config_paths(
        before_config, read_yaml_mapping(config.config_path)
    )
    env_added, env_updated, env_removed = diff_env_keys(before_env, after_env)
    report_path = onboarding_report_detail_path(config.instance_root)
    report = OnboardingReport(
        mode="updated" if existing_config_found else "created",
        config_path=config.config_path,
        detail_path=report_path,
        config_added_paths=config_added,
        config_updated_paths=config_updated,
        config_removed_paths=config_removed,
        env_added_keys=env_added,
        env_updated_keys=env_updated,
        env_removed_keys=env_removed,
        created_paths=tuple(created_paths + [report_path]),
    )
    write_onboarding_report(report)
    print(config.config_path)
    for path in created_paths:
        print(path)
    _print_onboarding_report(report)
    if config.launch.secrets_from_environment:
        _print_environment_secrets_status(config, answers)


def _bootstrap_cli_onboarded_instance(
    config: GatewayInstanceConfig,
    answers: dict[str, Any],
    *,
    source_env_file_path: Optional[str],
    expected_config_revision: str | None,
) -> list[Path]:
    return bootstrap_instance(
        config,
        source_env_file_path=source_env_file_path,
        expected_config_revision=expected_config_revision,
        assistant_purpose=str(answers["assistant_purpose"]).strip() or None,
        assistant_response_style=str(answers["assistant_response_style"]).strip()
        or None,
        assistant_autonomy=str(answers["assistant_autonomy"]).strip() or None,
    )


def _write_cli_onboarding_env_file(
    config: GatewayInstanceConfig,
    answers: dict[str, Any],
    *,
    args: argparse.Namespace,
    seed: GatewayOnboardingSeed,
    expected_config_revision: str | None,
) -> tuple[list[Path], dict[str, str], dict[str, str]]:
    """Write the instance with its secrets in team.env (launch.secrets_source: env_file)."""

    env_path = (
        Path(config.launch.env_file_path or default_launch_env_file(config.instance_root))
        .expanduser()
        .resolve()
    )
    before_env = parse_env_file(env_path)
    created_paths = _bootstrap_cli_onboarded_instance(
        config, answers, source_env_file_path=seed.source_env_file_path,
        expected_config_revision=expected_config_revision,
    )
    _merge_photon_onboarding_env(env_path, answers)
    rotate = bool(getattr(args, "rotate_internal_credentials", False))
    if _should_generate_internal_credentials(args):
        generated_env = ensure_internal_credential_env_file(
            config,
            existing_postgres_dsn=seed.template.manager_storage_postgres_dsn,
            rotate=rotate,
        )
        if generated_env not in created_paths:
            created_paths.append(generated_env)
        for sealbox_path in ensure_instance_sealbox_material(
            config, rotate_token=rotate
        ):
            if sealbox_path not in created_paths:
                created_paths.append(sealbox_path)
    seed_default_gateway_user(config, env=parse_env_file(env_path))
    return created_paths, before_env, parse_env_file(env_path)


def _refuse_env_file_options_for_environment_secrets(
    args: argparse.Namespace, seed: GatewayOnboardingSeed
) -> None:
    """Options that write team.env contradict secrets from the environment; say so plainly."""

    conflicting = [
        flag
        for flag, given in (
            ("--base-env-file", bool(seed.source_env_file_path)),
            ("--generate-internal-credentials", getattr(args, "generate_internal_credentials", None) is True),
            ("--rotate-internal-credentials", bool(getattr(args, "rotate_internal_credentials", False))),
        )
        if given
    ]
    if conflicting:
        raise SystemExit(
            f"{', '.join(conflicting)} writes team.env, but this instance's secrets come from the "
            "environment (launch.secrets_source: environment), so no team.env is written. Leave "
            "the option out and add the secrets as environment variables or Docker secrets; "
            "python -m scripts.generate_internal_credentials --print prints new values without writing a file."
        )


def _write_cli_onboarding_with_environment_secrets(
    config: GatewayInstanceConfig,
    answers: dict[str, Any],
    *,
    expected_config_revision: str | None,
) -> tuple[list[Path], dict[str, str], dict[str, str]]:
    """Write the instance without team.env; the first user's storage settings come from the environment."""

    created_paths = _bootstrap_cli_onboarded_instance(
        config, answers, source_env_file_path=None, expected_config_revision=expected_config_revision
    )
    # The broker's own Sealbox store, not a secret the environment supplies.
    for sealbox_path in ensure_instance_sealbox_material(config):
        if sealbox_path not in created_paths:
            created_paths.append(sealbox_path)
    seed_default_gateway_user(config, env=environment_secret_values())
    return created_paths, {}, {}


def _print_environment_secrets_status(
    config: GatewayInstanceConfig, answers: dict[str, Any]
) -> None:
    entered = ["PHOTON_PROJECT_SECRET"] if str(answers.get("photon_project_secret") or "").strip() else []
    missing = missing_secrets(config, env=environment_secret_values(), entered_names=entered)
    print("Secrets come from the environment (launch.secrets_source: environment); no team.env was written.")
    if not missing:
        print("Every secret this configuration needs is in the environment.")
        return
    print("These secrets are missing from the environment. Add each as an environment variable or Docker secret:")
    print(missing_secrets_text(missing))


def _merge_photon_onboarding_env(env_path: Path, answers: dict[str, object]) -> None:
    values: dict[str, str] = {}
    for env_key, answer_key in (
        ("PHOTON_PROJECT_ID", "photon_project_id"),
        ("PHOTON_PROJECT_SECRET", "photon_project_secret"),
    ):
        value = str(answers.get(answer_key) or "").strip()
        if value:
            values[env_key] = value
    if values:
        merge_env_file(env_path, values, rotate=False)


async def _onboard_web(args: argparse.Namespace) -> None:
    _load_cli_env_file(getattr(args, "env", None))
    reject_removed_public_url_env_vars()
    from assistant_gateway.onboarding_web.server import (
        OnboardingWebSettings,
        run_onboarding_web,
    )

    settings = OnboardingWebSettings(
        host=str(args.host),
        port=int(args.port),
        instance_root=str(args.instance_root),
        base_config=args.base_config,
        base_env_file=args.base_env_file,
        restart_on_complete=bool(args.restart_on_complete),
    )
    await run_onboarding_web(settings)


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()

    if args.command == "onboard":
        _onboard(args)
        return
    if args.command == "onboard-web":
        asyncio.run(_onboard_web(args))
        return
    if args.command == "migrate-config":
        _migrate_config(args.config, assume_yes=args.yes)
        return
    if args.command == "access":
        _access_command(args)
        return
    if args.command == "memory":
        from custom_agents.pinned_memory_transfer import run_memory_command

        _load_cli_env_file(args.env)
        run_memory_command(args)
        return
    if args.command == "team":
        from team_transfer.cli import run_team_command

        _load_cli_env_file(getattr(args, "env", None))
        run_team_command(args)
        return
    if args.command == "mcp-operation-levels":
        _mcp_operation_levels(args)
        return
    if args.command == "openai-codex-status":
        _openai_codex_status(args)
        return
    if args.command == "openai-codex-import":
        _openai_codex_import(args)
        return
    if args.command == "openai-codex-models":
        _openai_codex_models(args)
        return
    if args.command == "xai-oauth-login":
        _xai_oauth_login(args)
        return
    if args.command == "xai-oauth-status":
        _xai_oauth_status(args)
        return
    if args.command == "chat":
        asyncio.run(
            _chat(
                _ensure_instance_config_ready(args.config),
                args.session_key,
                env_path=args.env,
            )
        )
        return
    if args.command == "bridge-chat":
        asyncio.run(
            _bridge_chat(
                _ensure_instance_config_ready(args.config),
                args.session_key,
                args.channel_name,
                args.chat_id,
                args.user_id,
                env_path=args.env,
            )
        )
        return
    if args.command == "run-gateway":
        asyncio.run(
            _run_gateway_services(
                _ensure_instance_config_ready(args.config),
                env_path=args.env,
                include_apps=None,
                disable_apps=bool(args.no_apps),
            )
        )
        return
    if args.command == "run-adapter-host":
        asyncio.run(
            _run_adapter_host_service(
                _ensure_instance_config_ready(args.config),
                env_path=args.env,
            )
        )
        return
    if args.command == "run-activity-collector":
        asyncio.run(
            _run_activity_collector_service(
                _ensure_instance_config_ready(args.config),
                env_path=args.env,
            )
        )
        return
    if args.command == "run-secure-tool-broker":
        asyncio.run(
            _run_secure_tool_broker_service(
                _ensure_instance_config_ready(args.config),
                env_path=args.env,
                broker_env_file=args.broker_env_file,
                host=args.host,
                port=args.port,
            )
        )
        return
    if args.command == "run-app":
        asyncio.run(
            _run_app(
                _ensure_instance_config_ready(args.config),
                app_name=args.app,
                env_path=args.env,
            )
        )
        return
    if args.command == "interactive-app-list":
        _interactive_app_list(
            _ensure_instance_config_ready(args.config),
            env_path=args.env,
        )
        return
    if args.command == "run-execution-controller":
        asyncio.run(
            _run_execution_controller_service(
                _ensure_instance_config_ready(args.config),
                env_path=args.env,
            )
        )
        return
    if args.command == "execution-job-list":
        args.config = _ensure_instance_config_ready(args.config)
        _execution_job_list(args)
        return
    if args.command == "execution-job-show":
        args.config = _ensure_instance_config_ready(args.config)
        _execution_job_show(args)
        return
    if args.command == "execution-retire-old-records":
        args.config = _ensure_instance_config_ready(args.config)
        _execution_retire_old_records(args)
        return
    if args.command == "cron-add":
        if args.interval_seconds is None and args.cron_expression is None:
            raise SystemExit(
                "cron-add requires --interval-seconds or --cron-expression"
            )
        args.config = _ensure_instance_config_ready(args.config)
        asyncio.run(_cron_add(args))
        return
    if args.command == "cron-list":
        asyncio.run(
            _cron_list(_ensure_instance_config_ready(args.config), env_path=args.env)
        )
        return
    if args.command == "cron-remove":
        asyncio.run(
            _cron_remove(
                _ensure_instance_config_ready(args.config),
                args.job_id,
                env_path=args.env,
            )
        )
        return
    if args.command == "cron-pause":
        asyncio.run(
            _cron_pause(
                _ensure_instance_config_ready(args.config),
                args.job_id,
                minutes=args.minutes,
                until_iso=args.until_iso,
                env_path=args.env,
            )
        )
        return
    if args.command == "cron-resume":
        asyncio.run(
            _cron_resume(
                _ensure_instance_config_ready(args.config),
                args.job_id,
                env_path=args.env,
            )
        )
        return
    if args.command == "cron-disable":
        asyncio.run(
            _cron_disable(
                _ensure_instance_config_ready(args.config),
                args.job_id,
                env_path=args.env,
            )
        )
        return
    if args.command == "objective-list":
        args.config = _ensure_instance_config_ready(args.config)
        _objective_list(args)
        return
    if args.command == "objective-show":
        args.config = _ensure_instance_config_ready(args.config)
        _objective_show(args)
        return
    if args.command == "heartbeat-pause":
        asyncio.run(
            _heartbeat_pause(
                _ensure_instance_config_ready(args.config),
                minutes=args.minutes,
                until_iso=args.until_iso,
                env_path=args.env,
            )
        )
        return
    if args.command == "heartbeat-resume":
        asyncio.run(
            _heartbeat_resume(
                _ensure_instance_config_ready(args.config), env_path=args.env
            )
        )
        return
    if args.command == "heartbeat-disable":
        asyncio.run(
            _heartbeat_disable(
                _ensure_instance_config_ready(args.config), env_path=args.env
            )
        )
        return
    if args.command == "deliver-pending":
        asyncio.run(
            _deliver_pending(
                _ensure_instance_config_ready(args.config), env_path=args.env
            )
        )
        return
    if args.command == "memory-graph-embedding-status":
        _memory_graph_embedding_status(
            _ensure_instance_config_ready(args.config), env_path=args.env
        )
        return
    if args.command == "memory-graph-embedding-reset":
        _memory_graph_embedding_reset(
            _ensure_instance_config_ready(args.config),
            env_path=args.env,
            archive=bool(args.archive),
            purge=bool(args.purge),
        )
        return
    raise SystemExit(f"Unhandled command: {args.command}")


if __name__ == "__main__":
    main()
