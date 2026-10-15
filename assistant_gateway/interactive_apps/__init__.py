"""Interactive app adapters built on top of the gateway bridge."""

from assistant_gateway.interactive_apps.base import BaseInteractiveApp
from assistant_gateway.interactive_apps.contracts import (
    INTERACTIVE_APP_PLUGIN_API_VERSION,
    InteractiveAppCapability,
    InteractiveAppDependencyReport,
    InteractiveAppDescriptor,
    InteractiveAppDiscoveryDiagnostic,
    InteractiveAppDiscoveryStatus,
    InteractiveAppHealth,
    InteractiveAppLifecycleState,
    InteractiveAppOnboardingMetadata,
    InteractiveAppPackageProvenance,
    InteractiveAppPluginOrigin,
)
from assistant_gateway.interactive_apps.registry import (
    InteractiveAppRegistrationError,
    InteractiveAppRegistry,
)


_LAZY_EXPORTS = {
    "DiscordInteractiveApp": (
        "assistant_gateway.interactive_apps.discord",
        "DiscordInteractiveApp",
    ),
    "InteractiveAppManager": (
        "assistant_gateway.interactive_apps.manager",
        "InteractiveAppManager",
    ),
    "PhotonInteractiveApp": (
        "assistant_gateway.interactive_apps.photon",
        "PhotonInteractiveApp",
    ),
    "SignalInteractiveApp": (
        "assistant_gateway.interactive_apps.signal",
        "SignalInteractiveApp",
    ),
    "TelegramInteractiveApp": (
        "assistant_gateway.interactive_apps.telegram",
        "TelegramInteractiveApp",
    ),
    "InternalWebsiteInteractiveApp": (
        "assistant_gateway.interactive_apps.website",
        "InternalWebsiteInteractiveApp",
    ),
    "WhatsAppInteractiveApp": (
        "assistant_gateway.interactive_apps.whatsapp",
        "WhatsAppInteractiveApp",
    ),
}

__all__ = [
    "BaseInteractiveApp",
    "DiscordInteractiveApp",
    "INTERACTIVE_APP_PLUGIN_API_VERSION",
    "InteractiveAppCapability",
    "InteractiveAppDependencyReport",
    "InteractiveAppDescriptor",
    "InteractiveAppDiscoveryDiagnostic",
    "InteractiveAppDiscoveryStatus",
    "InteractiveAppHealth",
    "InteractiveAppLifecycleState",
    "InteractiveAppManager",
    "InteractiveAppOnboardingMetadata",
    "InteractiveAppPackageProvenance",
    "InteractiveAppPluginOrigin",
    "InteractiveAppRegistrationError",
    "InteractiveAppRegistry",
    "PhotonInteractiveApp",
    "SignalInteractiveApp",
    "TelegramInteractiveApp",
    "InternalWebsiteInteractiveApp",
    "WhatsAppInteractiveApp",
]


def __getattr__(name: str):
    target = _LAZY_EXPORTS.get(name)
    if target is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    from importlib import import_module

    module_name, attribute_name = target
    return getattr(import_module(module_name), attribute_name)
