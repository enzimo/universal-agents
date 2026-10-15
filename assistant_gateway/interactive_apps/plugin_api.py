"""Versioned public import surface for in-process interactive-app packages.

Package plugins should import contracts from this module only. Gateway runtime,
configuration, persistence, transport, broker, and authorization modules are
deliberately absent from this surface.
"""

from assistant_gateway.interactive_apps.base import ActivityContext, BaseInteractiveApp
from assistant_gateway.interactive_apps.contracts import (
    INTERACTIVE_APP_PLUGIN_API_VERSION,
    InteractiveAppApplication,
    InteractiveAppBridge,
    InteractiveAppBuildContext,
    InteractiveAppCapability,
    InteractiveAppDependencyReport,
    InteractiveAppDescriptor,
    InteractiveAppOnboardingMetadata,
)

__all__ = [
    "ActivityContext",
    "BaseInteractiveApp",
    "INTERACTIVE_APP_PLUGIN_API_VERSION",
    "InteractiveAppApplication",
    "InteractiveAppBridge",
    "InteractiveAppBuildContext",
    "InteractiveAppCapability",
    "InteractiveAppDependencyReport",
    "InteractiveAppDescriptor",
    "InteractiveAppOnboardingMetadata",
]
