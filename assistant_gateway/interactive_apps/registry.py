"""Registry and construction rules for interactive app plugins."""

from __future__ import annotations

from collections.abc import Mapping

from assistant_gateway.interactive_apps.base import BaseInteractiveApp
from assistant_gateway.interactive_apps.contracts import (
    INTERACTIVE_APP_PLUGIN_API_VERSION,
    InteractiveAppBuildContext,
    InteractiveAppDescriptor,
    InteractiveAppDiscoveryDiagnostic,
    InteractiveAppPackageProvenance,
    InteractiveAppPluginOrigin,
)
from assistant_gateway.interactive_apps.identifiers import (
    INTERACTIVE_APP_ENTRY_POINT_GROUP,
    normalize_distribution_name,
    normalize_interactive_app_plugin_id,
)


class InteractiveAppRegistrationError(ValueError):
    """Raised when a descriptor violates registry invariants."""


class InteractiveAppRegistry:
    """Ordered registry with explicit trust and compatibility checks."""

    def __init__(
        self,
        *,
        allowed_optional_plugins: Mapping[str, str] | None = None,
    ) -> None:
        self._allowed_optional_plugins = {
            normalize_interactive_app_plugin_id(plugin_id): normalize_distribution_name(
                distribution_name
            )
            for plugin_id, distribution_name in (allowed_optional_plugins or {}).items()
        }
        self._descriptors_by_plugin_id: dict[str, InteractiveAppDescriptor] = {}
        self._descriptors_by_channel: dict[str, InteractiveAppDescriptor] = {}
        self._provenance_by_plugin_id: dict[str, InteractiveAppPackageProvenance] = {}
        self._discovery_diagnostics: list[InteractiveAppDiscoveryDiagnostic] = []

    @property
    def allowed_optional_plugin_ids(self) -> frozenset[str]:
        return frozenset(self._allowed_optional_plugins)

    @property
    def allowed_optional_plugins(self) -> dict[str, str]:
        return dict(self._allowed_optional_plugins)

    @property
    def descriptors(self) -> tuple[InteractiveAppDescriptor, ...]:
        return tuple(self._descriptors_by_plugin_id.values())

    @property
    def discovery_diagnostics(self) -> tuple[InteractiveAppDiscoveryDiagnostic, ...]:
        return tuple(self._discovery_diagnostics)

    def register_builtin(self, descriptor: InteractiveAppDescriptor) -> None:
        """Register a descriptor selected by the trusted built-in composition root."""
        self._register(
            descriptor,
            provenance=InteractiveAppPackageProvenance(
                origin=InteractiveAppPluginOrigin.BUNDLED
            ),
        )

    def register_optional(
        self,
        descriptor: InteractiveAppDescriptor,
        *,
        provenance: InteractiveAppPackageProvenance,
    ) -> None:
        """Register a non-bundled descriptor only when its id was allowlisted."""
        allowed_distribution = self._allowed_optional_plugins.get(descriptor.plugin_id)
        if allowed_distribution is None:
            raise InteractiveAppRegistrationError(
                f"Optional interactive app plugin {descriptor.plugin_id!r} is not allowlisted."
            )
        if provenance.origin != InteractiveAppPluginOrigin.PYTHON_ENTRY_POINT:
            raise InteractiveAppRegistrationError(
                f"Optional interactive app plugin {descriptor.plugin_id!r} must have "
                "Python entry-point provenance."
            )
        if provenance.entry_point_group != INTERACTIVE_APP_ENTRY_POINT_GROUP:
            raise InteractiveAppRegistrationError(
                f"Optional interactive app plugin {descriptor.plugin_id!r} must use entry-point "
                f"group {INTERACTIVE_APP_ENTRY_POINT_GROUP!r}."
            )
        if provenance.entry_point_name != descriptor.plugin_id:
            raise InteractiveAppRegistrationError(
                f"Optional interactive app plugin {descriptor.plugin_id!r} entry-point name "
                "does not match its plugin id."
            )
        try:
            actual_distribution = normalize_distribution_name(
                provenance.distribution_name
            )
        except ValueError as exc:
            raise InteractiveAppRegistrationError(
                f"Optional interactive app plugin {descriptor.plugin_id!r} has no valid "
                "distribution provenance."
            ) from exc
        if actual_distribution != allowed_distribution:
            raise InteractiveAppRegistrationError(
                f"Optional interactive app plugin {descriptor.plugin_id!r} distribution "
                f"{actual_distribution!r} does not match the allowlisted distribution "
                f"{allowed_distribution!r}."
            )
        self._register(descriptor, provenance=provenance)

    def record_discovery_diagnostic(
        self,
        diagnostic: InteractiveAppDiscoveryDiagnostic,
    ) -> None:
        """Record a package-discovery result without manufacturing a descriptor."""
        self._discovery_diagnostics.append(diagnostic)

    def descriptor_for_channel(
        self, channel_name: str
    ) -> InteractiveAppDescriptor | None:
        return self._descriptors_by_channel.get(str(channel_name or "").strip())

    def descriptor_for_plugin(self, plugin_id: str) -> InteractiveAppDescriptor | None:
        return self._descriptors_by_plugin_id.get(str(plugin_id or "").strip())

    def provenance_for_plugin(
        self,
        plugin_id: str,
    ) -> InteractiveAppPackageProvenance | None:
        return self._provenance_by_plugin_id.get(str(plugin_id or "").strip())

    def create(
        self,
        descriptor: InteractiveAppDescriptor,
        context: InteractiveAppBuildContext,
    ) -> BaseInteractiveApp:
        registered = self.descriptor_for_plugin(descriptor.plugin_id)
        if registered is not descriptor:
            raise InteractiveAppRegistrationError(
                f"Interactive app plugin {descriptor.plugin_id!r} is not registered in this registry."
            )
        app = descriptor.factory(context)
        if not isinstance(app, BaseInteractiveApp):
            raise InteractiveAppRegistrationError(
                f"Factory for interactive app plugin {descriptor.plugin_id!r} "
                "must return BaseInteractiveApp."
            )
        if app.channel_name != descriptor.channel_name:
            raise InteractiveAppRegistrationError(
                f"Factory for interactive app plugin {descriptor.plugin_id!r} returned "
                f"channel {app.channel_name!r}; expected {descriptor.channel_name!r}."
            )
        return app

    def _register(
        self,
        descriptor: InteractiveAppDescriptor,
        *,
        provenance: InteractiveAppPackageProvenance,
    ) -> None:
        normalized_plugin_id = normalize_interactive_app_plugin_id(descriptor.plugin_id)
        if normalized_plugin_id != descriptor.plugin_id:
            raise InteractiveAppRegistrationError(
                f"Interactive app plugin id {descriptor.plugin_id!r} is not canonical; "
                f"use {normalized_plugin_id!r}."
            )
        if descriptor.plugin_api_version != INTERACTIVE_APP_PLUGIN_API_VERSION:
            raise InteractiveAppRegistrationError(
                f"Interactive app plugin {descriptor.plugin_id!r} uses API version "
                f"{descriptor.plugin_api_version!r}; this host supports "
                f"{INTERACTIVE_APP_PLUGIN_API_VERSION!r}."
            )
        if descriptor.plugin_id in self._descriptors_by_plugin_id:
            raise InteractiveAppRegistrationError(
                f"Interactive app plugin id {descriptor.plugin_id!r} is already registered."
            )
        existing = self._descriptors_by_channel.get(descriptor.channel_name)
        if existing is not None:
            raise InteractiveAppRegistrationError(
                f"Interactive app channel {descriptor.channel_name!r} is already owned by "
                f"plugin {existing.plugin_id!r}."
            )
        self._descriptors_by_plugin_id[descriptor.plugin_id] = descriptor
        self._descriptors_by_channel[descriptor.channel_name] = descriptor
        self._provenance_by_plugin_id[descriptor.plugin_id] = provenance
