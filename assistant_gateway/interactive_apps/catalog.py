"""Read-only adapter catalog shared by onboarding and operator surfaces."""

from __future__ import annotations

from dataclasses import dataclass

from runtime_settings.interactive_apps import InteractiveAppsSettings
from assistant_gateway.interactive_apps.contracts import (
    InteractiveAppDiscoveryStatus,
)
from assistant_gateway.interactive_apps.registry import InteractiveAppRegistry


@dataclass(frozen=True)
class InteractiveAppCatalogEntry:
    plugin_id: str
    channel_name: str | None
    display_name: str
    configured_enabled: bool
    discovery_status: str
    origin: str | None
    distribution_name: str | None
    distribution_version: str | None
    capabilities: tuple[str, ...]
    summary: str
    setup_order: int
    configuration_title: str
    configuration_schema: dict[str, object] | None
    secret_reference_names: tuple[str, ...]
    documentation_reference: str
    installation_hint: str
    diagnostic: str | None = None

    def as_dict(self) -> dict[str, object]:
        return {
            "plugin_id": self.plugin_id,
            "channel_name": self.channel_name,
            "display_name": self.display_name,
            "configured_enabled": self.configured_enabled,
            "discovery_status": self.discovery_status,
            "origin": self.origin,
            "distribution_name": self.distribution_name,
            "distribution_version": self.distribution_version,
            "capabilities": list(self.capabilities),
            "summary": self.summary,
            "setup_order": self.setup_order,
            "configuration_title": self.configuration_title,
            "configuration_schema": self.configuration_schema,
            "secret_reference_names": list(self.secret_reference_names),
            "documentation_reference": self.documentation_reference,
            "installation_hint": self.installation_hint,
            "diagnostic": self.diagnostic,
        }


def build_interactive_app_catalog(
    settings: InteractiveAppsSettings,
    registry: InteractiveAppRegistry,
) -> tuple[InteractiveAppCatalogEntry, ...]:
    """Project trusted descriptors and discovery results into one catalog."""
    entries: list[InteractiveAppCatalogEntry] = []
    descriptor_ids: set[str] = set()
    for descriptor in registry.descriptors:
        descriptor_ids.add(descriptor.plugin_id)
        configured = settings.plugin(descriptor.plugin_id)
        provenance = registry.provenance_for_plugin(descriptor.plugin_id)
        entries.append(
            InteractiveAppCatalogEntry(
                plugin_id=descriptor.plugin_id,
                channel_name=descriptor.channel_name,
                display_name=descriptor.display_name,
                configured_enabled=bool(configured and configured.enabled),
                discovery_status=InteractiveAppDiscoveryStatus.REGISTERED.value,
                origin=provenance.origin.value if provenance is not None else None,
                distribution_name=(
                    provenance.distribution_name if provenance is not None else None
                ),
                distribution_version=(
                    provenance.distribution_version if provenance is not None else None
                ),
                capabilities=tuple(
                    sorted(item.value for item in descriptor.capabilities)
                ),
                summary=descriptor.onboarding.summary,
                setup_order=descriptor.onboarding.setup_order,
                configuration_title=descriptor.onboarding.configuration_title,
                configuration_schema=descriptor.configuration_schema(),
                secret_reference_names=descriptor.secret_reference_names,
                documentation_reference=descriptor.documentation_reference,
                installation_hint=descriptor.onboarding.installation_hint,
            )
        )

    for diagnostic in registry.discovery_diagnostics:
        if diagnostic.plugin_id in descriptor_ids:
            continue
        entries.append(
            InteractiveAppCatalogEntry(
                plugin_id=diagnostic.plugin_id,
                channel_name=None,
                display_name=diagnostic.plugin_id,
                configured_enabled=diagnostic.enabled,
                discovery_status=diagnostic.status.value,
                origin=None,
                distribution_name=diagnostic.distribution_name,
                distribution_version=None,
                capabilities=(),
                summary="Configured optional interactive app package.",
                setup_order=10_000,
                configuration_title="Settings",
                configuration_schema=None,
                secret_reference_names=(),
                documentation_reference="",
                installation_hint=(
                    f"Install the explicitly approved distribution "
                    f"{diagnostic.distribution_name!r}."
                ),
                diagnostic=diagnostic.detail,
            )
        )
    return tuple(sorted(entries, key=lambda item: (item.setup_order, item.plugin_id)))
