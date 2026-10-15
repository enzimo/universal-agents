"""Bundled registrations and the interactive-app composition root.

Adapter module names remain strings so registry construction does not import an
unused platform SDK. External package entry points are loaded only after the
instance configuration has allowlisted an exact plugin id and distribution.
"""

from __future__ import annotations

from dataclasses import dataclass
from importlib import import_module
from importlib.util import find_spec

from runtime_settings.interactive_apps import DiscordAppSettings, InternalWebsiteAppSettings, PhotonAppSettings, SignalAppSettings, TelegramAppSettings, WhatsAppAppSettings
from assistant_gateway.config import GatewayInstanceConfig
from assistant_gateway.interactive_apps.contracts import (
    INTERACTIVE_APP_PLUGIN_API_VERSION,
    InteractiveAppBuildContext,
    InteractiveAppCapability,
    InteractiveAppDependencyReport,
    InteractiveAppDependencyProbe,
    InteractiveAppDescriptor,
    InteractiveAppOnboardingMetadata,
)
from assistant_gateway.interactive_apps.base import BaseInteractiveApp
from assistant_gateway.interactive_apps.discovery import (
    EntryPointProvider,
    InteractiveAppPackageSelection,
    discover_optional_interactive_apps,
)
from assistant_gateway.interactive_apps.identifiers import (
    BUILTIN_DISCORD_PLUGIN_ID,
    BUILTIN_INTERNAL_WEBSITE_PLUGIN_ID,
    BUILTIN_PHOTON_PLUGIN_ID,
    BUILTIN_SIGNAL_PLUGIN_ID,
    BUILTIN_TELEGRAM_PLUGIN_ID,
    BUILTIN_WHATSAPP_PLUGIN_ID,
)
from assistant_gateway.interactive_apps.registry import InteractiveAppRegistry
from custom_messages.contracts.runtime_v1 import (
    DELIVERY_CHANNEL_DISCORD,
    DELIVERY_CHANNEL_PHOTON,
    DELIVERY_CHANNEL_SIGNAL,
    DELIVERY_CHANNEL_TELEGRAM,
    DELIVERY_CHANNEL_WHATSAPP,
    DELIVERY_CHANNEL_WEBSITE,
)


BUILTIN_ADAPTER_IMPLEMENTATION_VERSION = "1"


@dataclass(frozen=True)
class _LazyAdapterFactory:
    module_name: str
    class_name: str

    def __call__(self, context: InteractiveAppBuildContext) -> BaseInteractiveApp:
        module = import_module(self.module_name)
        adapter_class = getattr(module, self.class_name)
        return adapter_class(
            context.configuration,
            context.application,
            context.bridge,
            context.runtime,
        )


def _python_dependencies(
    *requirements: tuple[str, str],
    deferred_checks: tuple[str, ...] = (),
) -> InteractiveAppDependencyProbe:
    def probe(configuration: object) -> InteractiveAppDependencyReport:
        del configuration
        missing = tuple(
            package_name
            for import_name, package_name in requirements
            if find_spec(import_name) is None
        )
        checked = tuple(package_name for _import_name, package_name in requirements)
        if missing:
            return InteractiveAppDependencyReport(
                available=False,
                checked=checked,
                missing=missing,
                deferred_checks=deferred_checks,
                installation_hint=(
                    "Install the project's pinned adapter dependencies before "
                    "enabling this plugin."
                ),
                detail=f"Missing Python package(s): {', '.join(missing)}.",
            )
        return InteractiveAppDependencyReport(
            available=True,
            checked=checked,
            deferred_checks=deferred_checks,
            detail="All locally importable Python dependencies are available.",
        )

    return probe


_COMMON_TEXT_CAPABILITIES = frozenset(
    {
        InteractiveAppCapability.INBOUND_TEXT,
        InteractiveAppCapability.OUTBOUND_TEXT,
        InteractiveAppCapability.PROACTIVE_DELIVERY,
        # Every bundled channel adapter routes slash commands through the
        # gateway-owned command application. External registration must make
        # that authority-bearing capability explicit.
        InteractiveAppCapability.NATIVE_COMMANDS,
    }
)
_MEDIA_CAPABILITIES = frozenset(
    {
        InteractiveAppCapability.ATTACHMENTS,
        InteractiveAppCapability.IMAGES,
        InteractiveAppCapability.AUDIO_VOICE,
    }
)


BUILTIN_INTERACTIVE_APP_DESCRIPTORS = (
    InteractiveAppDescriptor(
        plugin_id=BUILTIN_DISCORD_PLUGIN_ID,
        channel_name=DELIVERY_CHANNEL_DISCORD,
        display_name="Discord",
        plugin_api_version=INTERACTIVE_APP_PLUGIN_API_VERSION,
        implementation_version=BUILTIN_ADAPTER_IMPLEMENTATION_VERSION,
        capabilities=_COMMON_TEXT_CAPABILITIES
        | _MEDIA_CAPABILITIES
        | frozenset(
            {
                InteractiveAppCapability.THREADS_TOPICS,
                InteractiveAppCapability.GROUP_CONVERSATIONS,
                InteractiveAppCapability.RICH_TEXT,
                InteractiveAppCapability.NATIVE_ACTIVITY_INDICATOR,
            }
        ),
        configuration_model=DiscordAppSettings,
        dependency_probe=_python_dependencies(
            ("discord", "discord.py"),
            deferred_checks=(
                "bot token and Discord gateway connectivity are checked during start",
            ),
        ),
        factory=_LazyAdapterFactory(
            "assistant_gateway.interactive_apps.discord",
            "DiscordInteractiveApp",
        ),
        onboarding=InteractiveAppOnboardingMetadata(
            summary="Connect a Discord bot for direct messages, channels, and threads.",
            setup_order=20,
            installation_hint="Install the pinned Discord adapter dependencies.",
        ),
        secret_reference_names=("DISCORD_BOT_TOKEN",),
        documentation_reference="user_docs_source/channels/discord.md",
    ),
    InteractiveAppDescriptor(
        plugin_id=BUILTIN_TELEGRAM_PLUGIN_ID,
        channel_name=DELIVERY_CHANNEL_TELEGRAM,
        display_name="Telegram",
        plugin_api_version=INTERACTIVE_APP_PLUGIN_API_VERSION,
        implementation_version=BUILTIN_ADAPTER_IMPLEMENTATION_VERSION,
        capabilities=_COMMON_TEXT_CAPABILITIES
        | _MEDIA_CAPABILITIES
        | frozenset(
            {
                InteractiveAppCapability.THREADS_TOPICS,
                InteractiveAppCapability.GROUP_CONVERSATIONS,
                InteractiveAppCapability.RICH_TEXT,
                InteractiveAppCapability.NATIVE_ACTIVITY_INDICATOR,
                InteractiveAppCapability.POLLING_LIFECYCLE,
            }
        ),
        configuration_model=TelegramAppSettings,
        dependency_probe=_python_dependencies(
            ("telegram", "python-telegram-bot"),
            deferred_checks=(
                "bot token and Telegram API connectivity are checked during start",
            ),
        ),
        factory=_LazyAdapterFactory(
            "assistant_gateway.interactive_apps.telegram",
            "TelegramInteractiveApp",
        ),
        onboarding=InteractiveAppOnboardingMetadata(
            summary="Connect a Telegram bot for private chats, groups, and topics.",
            setup_order=10,
            installation_hint="Install the pinned Telegram adapter dependencies.",
        ),
        secret_reference_names=("TELEGRAM_BOT_TOKEN",),
        documentation_reference="user_docs_source/channels/telegram.md",
    ),
    InteractiveAppDescriptor(
        plugin_id=BUILTIN_WHATSAPP_PLUGIN_ID,
        channel_name=DELIVERY_CHANNEL_WHATSAPP,
        display_name="WhatsApp",
        plugin_api_version=INTERACTIVE_APP_PLUGIN_API_VERSION,
        implementation_version=BUILTIN_ADAPTER_IMPLEMENTATION_VERSION,
        capabilities=_COMMON_TEXT_CAPABILITIES
        | _MEDIA_CAPABILITIES
        | frozenset({InteractiveAppCapability.WEBHOOK_LIFECYCLE}),
        configuration_model=WhatsAppAppSettings,
        dependency_probe=_python_dependencies(
            ("aiohttp", "aiohttp"),
            ("twilio", "twilio"),
            deferred_checks=(
                "Twilio credentials and webhook listener binding are checked during start",
            ),
        ),
        factory=_LazyAdapterFactory(
            "assistant_gateway.interactive_apps.whatsapp",
            "WhatsAppInteractiveApp",
        ),
        onboarding=InteractiveAppOnboardingMetadata(
            summary="Connect WhatsApp through an authenticated Twilio webhook.",
            setup_order=50,
            installation_hint="Install the pinned WhatsApp adapter dependencies.",
        ),
        secret_reference_names=(
            "TWILIO_ACCOUNT_SID",
            "TWILIO_AUTH_TOKEN",
            "TWILIO_WHATSAPP_FROM",
        ),
        documentation_reference="user_docs_source/channels/whatsapp.md",
    ),
    InteractiveAppDescriptor(
        plugin_id=BUILTIN_SIGNAL_PLUGIN_ID,
        channel_name=DELIVERY_CHANNEL_SIGNAL,
        display_name="Signal",
        plugin_api_version=INTERACTIVE_APP_PLUGIN_API_VERSION,
        implementation_version=BUILTIN_ADAPTER_IMPLEMENTATION_VERSION,
        capabilities=_COMMON_TEXT_CAPABILITIES
        | _MEDIA_CAPABILITIES
        | frozenset(
            {
                InteractiveAppCapability.GROUP_CONVERSATIONS,
                InteractiveAppCapability.RICH_TEXT,
                InteractiveAppCapability.DELIVERY_RECEIPTS,
                InteractiveAppCapability.POLLING_LIFECYCLE,
            }
        ),
        configuration_model=SignalAppSettings,
        dependency_probe=_python_dependencies(
            ("aiohttp", "aiohttp"),
            deferred_checks=(
                "configured signal-cli-rest-api availability is checked during start",
            ),
        ),
        factory=_LazyAdapterFactory(
            "assistant_gateway.interactive_apps.signal",
            "SignalInteractiveApp",
        ),
        onboarding=InteractiveAppOnboardingMetadata(
            summary="Connect an existing signal-cli-rest-api account.",
            setup_order=30,
            installation_hint="Run an authenticated signal-cli-rest-api service.",
        ),
        documentation_reference="user_docs_source/channels/signal.md",
    ),
    InteractiveAppDescriptor(
        plugin_id=BUILTIN_PHOTON_PLUGIN_ID,
        channel_name=DELIVERY_CHANNEL_PHOTON,
        display_name="Photon/iMessage",
        plugin_api_version=INTERACTIVE_APP_PLUGIN_API_VERSION,
        implementation_version=BUILTIN_ADAPTER_IMPLEMENTATION_VERSION,
        capabilities=_COMMON_TEXT_CAPABILITIES
        | _MEDIA_CAPABILITIES
        | frozenset(
            {
                InteractiveAppCapability.GROUP_CONVERSATIONS,
                InteractiveAppCapability.RICH_TEXT,
                InteractiveAppCapability.NATIVE_ACTIVITY_INDICATOR,
                InteractiveAppCapability.POLLING_LIFECYCLE,
            }
        ),
        configuration_model=PhotonAppSettings,
        dependency_probe=_python_dependencies(
            ("aiohttp", "aiohttp"),
            deferred_checks=(
                "project credentials, Node.js, and sidecar packages are checked during start",
            ),
        ),
        factory=_LazyAdapterFactory(
            "assistant_gateway.interactive_apps.photon",
            "PhotonInteractiveApp",
        ),
        onboarding=InteractiveAppOnboardingMetadata(
            summary="Connect iMessage through the local Photon Spectrum sidecar.",
            setup_order=40,
            installation_hint="Install the bundled Photon sidecar packages.",
        ),
        secret_reference_names=(
            "PHOTON_PROJECT_ID",
            "PHOTON_PROJECT_SECRET",
            "PHOTON_SIDECAR_TOKEN",
        ),
        documentation_reference="user_docs_source/channels/photon.md",
    ),
    InteractiveAppDescriptor(
        plugin_id=BUILTIN_INTERNAL_WEBSITE_PLUGIN_ID,
        channel_name=DELIVERY_CHANNEL_WEBSITE,
        display_name="Internal Website and Web Chat",
        plugin_api_version=INTERACTIVE_APP_PLUGIN_API_VERSION,
        implementation_version=BUILTIN_ADAPTER_IMPLEMENTATION_VERSION,
        capabilities=_COMMON_TEXT_CAPABILITIES
        | _MEDIA_CAPABILITIES
        | frozenset(
            {
                InteractiveAppCapability.RICH_TEXT,
                InteractiveAppCapability.WEBHOOK_LIFECYCLE,
            }
        ),
        configuration_model=InternalWebsiteAppSettings,
        dependency_probe=_python_dependencies(
            ("aiohttp", "aiohttp"),
            deferred_checks=(
                "website listener binding and configured document paths are checked during start",
            ),
        ),
        factory=_LazyAdapterFactory(
            "assistant_gateway.interactive_apps.website",
            "InternalWebsiteInteractiveApp",
        ),
        onboarding=InteractiveAppOnboardingMetadata(
            summary="Serve local documentation, generated sites, and authenticated Web Chat.",
            setup_order=0,
        ),
        secret_reference_names=("WEB_CHAT_AUTH_TOKEN",),
        documentation_reference="QUICKSTART.md",
    ),
)


def build_interactive_app_registry(
    config: GatewayInstanceConfig | None = None,
    *,
    entry_point_provider: EntryPointProvider | None = None,
) -> InteractiveAppRegistry:
    """Build the bundled registry and discover configured optional packages."""
    allowed_optional_plugins = (
        {
            plugin_id: config.interactive_apps.require_plugin(plugin_id).distribution
            or ""
            for plugin_id in config.interactive_apps.optional_plugin_ids
        }
        if config is not None
        else {}
    )
    registry = InteractiveAppRegistry(
        allowed_optional_plugins=allowed_optional_plugins,
    )
    for descriptor in BUILTIN_INTERACTIVE_APP_DESCRIPTORS:
        registry.register_builtin(descriptor)

    if config is None or not allowed_optional_plugins:
        return registry
    selections = tuple(
        InteractiveAppPackageSelection(
            plugin_id=plugin_id,
            distribution_name=config.interactive_apps.require_plugin(
                plugin_id
            ).distribution
            or "",
            enabled=config.interactive_apps.require_plugin(plugin_id).enabled,
        )
        for plugin_id in allowed_optional_plugins
    )
    discovery_kwargs = (
        {"entry_point_provider": entry_point_provider}
        if entry_point_provider is not None
        else {}
    )
    discover_optional_interactive_apps(registry, selections, **discovery_kwargs)
    return registry
