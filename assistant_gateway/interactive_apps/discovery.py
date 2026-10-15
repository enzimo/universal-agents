"""Allowlisted Python entry-point discovery for interactive app plugins."""

from __future__ import annotations

import logging
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from importlib import metadata
from typing import Protocol, cast

from assistant_gateway.interactive_apps.contracts import (
    InteractiveAppDescriptor,
    InteractiveAppDiscoveryDiagnostic,
    InteractiveAppDiscoveryStatus,
    InteractiveAppPackageProvenance,
    InteractiveAppPluginOrigin,
)
from assistant_gateway.interactive_apps.identifiers import (
    INTERACTIVE_APP_ENTRY_POINT_GROUP,
    normalize_distribution_name,
)
from assistant_gateway.interactive_apps.registry import InteractiveAppRegistry

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class InteractiveAppPackageSelection:
    """Secret-free operator selection passed into package discovery."""

    plugin_id: str
    distribution_name: str
    enabled: bool


class _Distribution(Protocol):
    @property
    def name(self) -> str: ...

    @property
    def version(self) -> str: ...


class _EntryPoint(Protocol):
    name: str
    value: str
    group: str
    dist: _Distribution | None

    def load(self) -> object: ...


EntryPointProvider = Callable[..., Iterable[_EntryPoint]]


def _installed_entry_points(*, group: str) -> Iterable[_EntryPoint]:
    return cast(Iterable[_EntryPoint], metadata.entry_points(group=group))


def discover_optional_interactive_apps(
    registry: InteractiveAppRegistry,
    selections: Iterable[InteractiveAppPackageSelection],
    *,
    entry_point_provider: EntryPointProvider = _installed_entry_points,
) -> None:
    """Discover configured packages without importing unallowlisted plugins."""
    discovered = tuple(entry_point_provider(group=INTERACTIVE_APP_ENTRY_POINT_GROUP))
    by_name: dict[str, list[_EntryPoint]] = {}
    for entry_point in discovered:
        by_name.setdefault(str(entry_point.name), []).append(entry_point)

    for selection in selections:
        if not selection.enabled:
            _record(
                registry,
                selection,
                InteractiveAppDiscoveryStatus.DISABLED,
                "Configured package is disabled; its entry point was not loaded.",
            )
            continue
        candidates = by_name.get(selection.plugin_id, [])
        if not candidates:
            _record(
                registry,
                selection,
                InteractiveAppDiscoveryStatus.MISSING,
                "No installed entry point matched this plugin id in group "
                f"{INTERACTIVE_APP_ENTRY_POINT_GROUP!r}. Install the explicitly "
                f"approved distribution {selection.distribution_name!r} and restart.",
            )
            continue
        if len(candidates) != 1:
            _record(
                registry,
                selection,
                InteractiveAppDiscoveryStatus.INVALID_DESCRIPTOR,
                f"Found {len(candidates)} matching entry points; exactly one is required.",
            )
            continue

        entry_point = candidates[0]
        installed_distribution = _entry_point_distribution_name(entry_point)
        if installed_distribution != selection.distribution_name:
            _record(
                registry,
                selection,
                InteractiveAppDiscoveryStatus.PROVENANCE_MISMATCH,
                "Entry-point distribution mismatch: configured "
                f"{selection.distribution_name!r}, installed "
                f"{installed_distribution or '(unknown)'!r}. The entry point was not loaded.",
            )
            continue

        provenance = InteractiveAppPackageProvenance(
            origin=InteractiveAppPluginOrigin.PYTHON_ENTRY_POINT,
            distribution_name=installed_distribution,
            distribution_version=_entry_point_distribution_version(entry_point),
            entry_point_group=INTERACTIVE_APP_ENTRY_POINT_GROUP,
            entry_point_name=str(entry_point.name),
            entry_point_value=str(entry_point.value),
        )
        try:
            provider = entry_point.load()
        except Exception as exc:
            _record(
                registry,
                selection,
                InteractiveAppDiscoveryStatus.LOAD_FAILED,
                f"Entry-point import failed: {type(exc).__name__}: {exc}",
            )
            continue
        if not callable(provider):
            _record(
                registry,
                selection,
                InteractiveAppDiscoveryStatus.INVALID_DESCRIPTOR,
                "Entry point must load a zero-argument descriptor provider callable.",
            )
            continue
        try:
            descriptor = provider()
            if not isinstance(descriptor, InteractiveAppDescriptor):
                raise TypeError(
                    "descriptor provider did not return InteractiveAppDescriptor"
                )
            if descriptor.plugin_id != selection.plugin_id:
                raise ValueError(
                    f"descriptor id {descriptor.plugin_id!r} does not match entry-point "
                    f"name {selection.plugin_id!r}"
                )
            registry.register_optional(descriptor, provenance=provenance)
        except Exception as exc:
            _record(
                registry,
                selection,
                InteractiveAppDiscoveryStatus.INVALID_DESCRIPTOR,
                f"Descriptor registration failed: {type(exc).__name__}: {exc}",
            )
            continue
        _record(
            registry,
            selection,
            InteractiveAppDiscoveryStatus.REGISTERED,
            "Allowlisted entry point registered successfully.",
        )


def _entry_point_distribution_name(entry_point: _EntryPoint) -> str | None:
    distribution = getattr(entry_point, "dist", None)
    raw_name = getattr(distribution, "name", None)
    if not raw_name:
        return None
    try:
        return normalize_distribution_name(raw_name)
    except ValueError:
        return None


def _entry_point_distribution_version(entry_point: _EntryPoint) -> str | None:
    distribution = getattr(entry_point, "dist", None)
    version = str(getattr(distribution, "version", "") or "").strip()
    return version or None


def _record(
    registry: InteractiveAppRegistry,
    selection: InteractiveAppPackageSelection,
    status: InteractiveAppDiscoveryStatus,
    detail: str,
) -> None:
    diagnostic = InteractiveAppDiscoveryDiagnostic(
        plugin_id=selection.plugin_id,
        distribution_name=selection.distribution_name,
        enabled=selection.enabled,
        status=status,
        detail=detail,
    )
    registry.record_discovery_diagnostic(diagnostic)
    log = (
        logger.info
        if status
        in {
            InteractiveAppDiscoveryStatus.REGISTERED,
            InteractiveAppDiscoveryStatus.DISABLED,
        }
        else logger.warning
    )
    log(
        "Interactive app package discovery completed. "
        "event=interactive_app_package_discovery plugin_id=%s distribution=%s "
        "enabled=%s status=%s detail=%s",
        selection.plugin_id,
        selection.distribution_name,
        selection.enabled,
        status.value,
        detail,
    )
