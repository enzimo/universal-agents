"""Manager for interactive app adapters."""

from __future__ import annotations

import inspect
import logging
from dataclasses import dataclass
from typing import Awaitable, Callable, Optional, Sequence

from assistant_gateway.application.gateway_application import (
    GatewayApplication,
    GatewayDeliveryEvent,
)
from assistant_gateway.application.website_application import WebsiteGatewayApplication
from assistant_gateway.attachments import gateway_staged_attachment_reader
from assistant_gateway.channel_bridge import ChannelBridge
from assistant_gateway.config import GatewayInstanceConfig
from runtime_settings.interactive_apps import INTERACTIVE_APP_EXECUTION_ADAPTER_HOST
from assistant_gateway.interactive_apps.base import ActivityContext, BaseInteractiveApp
from assistant_gateway.interactive_apps.builtin_registry import (
    build_interactive_app_registry,
)
from assistant_gateway.interactive_apps.contracts import (
    InteractiveAppBuildContext,
    InteractiveAppDependencyReport,
    InteractiveAppDescriptor,
    InteractiveAppHealth,
    InteractiveAppLifecycleState,
    InteractiveAppPackageProvenance,
)
from assistant_gateway.interactive_apps.registry import (
    InteractiveAppRegistry,
)
from assistant_gateway.interactive_apps.host_ports import (
    InternalWebsiteHostEnvironment,
    InteractiveAppRuntimeContext,
    LocalInteractiveAppAttachmentStore,
)
from assistant_gateway.inbound_attachments import (
    InboundAttachmentStoragePolicy,
    resolve_inbound_attachment_cache_dir,
)
from assistant_gateway.voice import build_voice_client
from assistant_gateway.config import resolve_activity_collector_runtime_base_url

logger = logging.getLogger(__name__)

OutputSink = Callable[[str], Optional[Awaitable[None]] | None]


@dataclass
class _PluginRuntimeRecord:
    descriptor: InteractiveAppDescriptor
    provenance: InteractiveAppPackageProvenance
    configured_enabled: bool | None = None
    selected: bool = False
    state: InteractiveAppLifecycleState = InteractiveAppLifecycleState.DISABLED
    dependency_report: InteractiveAppDependencyReport | None = None
    error: str | None = None

    def health(self) -> InteractiveAppHealth:
        return InteractiveAppHealth(
            plugin_id=self.descriptor.plugin_id,
            channel_name=self.descriptor.channel_name,
            display_name=self.descriptor.display_name,
            state=self.state,
            configured_enabled=self.configured_enabled,
            selected=self.selected,
            capabilities=self.descriptor.capabilities,
            provenance=self.provenance,
            dependency_report=self.dependency_report,
            error=self.error,
        )


class _UnavailableInteractiveApp(BaseInteractiveApp):
    """Enabled placeholder that preserves normal manager failure semantics."""

    def __init__(
        self,
        *,
        channel_name: str,
        application: GatewayApplication,
        error: Exception,
    ) -> None:
        self.channel_name = channel_name
        self.application = application
        self._error = error

    @property
    def enabled(self) -> bool:
        return True

    async def start(self) -> None:
        raise self._error

    async def stop(self) -> None:
        return None

    async def deliver_event(self, _event: GatewayDeliveryEvent) -> None:
        raise RuntimeError(f"Interactive app {self.channel_name!r} is not running.")

    async def emit_text_activity_update(
        self,
        _context: ActivityContext,
        _text: str,
    ) -> None:
        return None


class InteractiveAppManager:
    """Start enabled interactive apps and route gateway delivery events to them."""

    def __init__(
        self,
        config: GatewayInstanceConfig,
        application: WebsiteGatewayApplication,
        *,
        output_sink: Optional[OutputSink] = None,
        include_apps: Optional[Sequence[str]] = None,
        strict_startup: Optional[bool] = None,
        registry: InteractiveAppRegistry | None = None,
    ) -> None:
        self.config = config
        self.output_sink = output_sink
        self.gateway_application = application
        self.bridge = ChannelBridge(self.gateway_application)
        attachment_settings = config.interactive_apps.internal_website.attachments
        self.runtime = InteractiveAppRuntimeContext(
            request_timeout_seconds=config.manager.request_timeout_seconds,
            voice=config.voice,
            voice_client=build_voice_client(config.voice),
            authorization_presentation_mode=config.authorization.presentation.mode,
            attachments=LocalInteractiveAppAttachmentStore(
                InboundAttachmentStoragePolicy(
                    cache_dir=resolve_inbound_attachment_cache_dir(config),
                    max_file_bytes=attachment_settings.max_file_bytes,
                    allowed_kinds=frozenset(attachment_settings.allowed_kinds),
                ),
                self.gateway_application,
            ),
            outgoing_attachments=gateway_staged_attachment_reader(config),
            website=InternalWebsiteHostEnvironment(
                instance_root_path=config.instance_root_path,
                workspace_path=config.workspace_path,
                config_path=config.config_path.resolve(),
                base_url=config.internal_website_base_url,
                public_origin=config.network.public_origin,
                assistant_display_name=config.assistant.display_name,
                appearance=config.ui.onboarding.appearance.model_dump(mode="json"),
                activity_collector_base_url=(
                    resolve_activity_collector_runtime_base_url(config) or ""
                ),
                activity_collector_admin_token=(
                    config.activity_collector.resolve_admin_token()
                ),
                activity_collector_request_timeout_seconds=(
                    config.activity_collector.request_timeout_seconds
                ),
                agent_debug=dict(config.agent.debug or {}),
            ),
        )
        self.registry = registry or build_interactive_app_registry(config)
        self.include_apps = self._resolve_included_channels(include_apps or ())
        self.strict_startup = (
            bool(include_apps) if strict_startup is None else bool(strict_startup)
        )
        self.apps: dict[str, BaseInteractiveApp] = {}
        self._plugin_records: dict[str, _PluginRuntimeRecord] = {}
        self._running_app_names: set[str] = set()
        self._build_apps()

    def _resolve_included_channels(self, selectors: Sequence[str]) -> set[str]:
        channels: set[str] = set()
        for raw_selector in selectors:
            selector = str(raw_selector or "").strip()
            descriptor = self.registry.descriptor_for_plugin(selector)
            if descriptor is None:
                descriptor = self.registry.descriptor_for_channel(selector)
            if descriptor is None:
                raise ValueError(
                    f"Unknown interactive app selector {selector!r}; use a registered "
                    "plugin id or channel name."
                )
            channels.add(descriptor.channel_name)
        return channels

    def _build_apps(self) -> None:
        for descriptor in self.registry.descriptors:
            provenance = self.registry.provenance_for_plugin(descriptor.plugin_id)
            if provenance is None:
                raise RuntimeError(
                    f"Interactive app plugin {descriptor.plugin_id!r} has no trusted provenance."
                )
            record = _PluginRuntimeRecord(
                descriptor=descriptor,
                provenance=provenance,
            )
            self._plugin_records[descriptor.channel_name] = record
            self._build_registered_app(record)

    def _build_registered_app(
        self,
        record: _PluginRuntimeRecord,
    ) -> None:
        descriptor = record.descriptor
        record.selected = (
            not self.include_apps or descriptor.channel_name in self.include_apps
        )
        configured_plugin = self.config.interactive_apps.plugin(descriptor.plugin_id)
        if configured_plugin is None:
            record.error = (
                "Plugin is registered but has no normalized configuration entry."
            )
            record.state = InteractiveAppLifecycleState.FAILED
            if record.selected:
                self.apps[descriptor.channel_name] = _UnavailableInteractiveApp(
                    channel_name=descriptor.channel_name,
                    application=self.gateway_application,
                    error=RuntimeError(
                        f"Interactive app plugin {descriptor.plugin_id!r} must be configured at "
                        f"interactive_apps.plugins.{descriptor.plugin_id}."
                    ),
                )
            return

        record.configured_enabled = configured_plugin.enabled

        if not record.configured_enabled:
            record.state = InteractiveAppLifecycleState.DISABLED
            return
        if not record.selected:
            record.state = InteractiveAppLifecycleState.NOT_SELECTED
            return
        if configured_plugin.execution == INTERACTIVE_APP_EXECUTION_ADAPTER_HOST:
            record.state = InteractiveAppLifecycleState.HOSTED_EXTERNALLY
            return

        try:
            raw_configuration = configured_plugin.settings
            model_dump = getattr(raw_configuration, "model_dump", None)
            if callable(model_dump) and not isinstance(
                raw_configuration,
                descriptor.configuration_model,
            ):
                raw_configuration = model_dump(mode="python")
            configuration = descriptor.validate_configuration(raw_configuration)
        except Exception as exc:
            error = RuntimeError(
                f"Interactive app plugin {descriptor.plugin_id!r} has invalid settings: {exc}"
            )
            record.state = InteractiveAppLifecycleState.FAILED
            record.error = str(error)
            self.apps[descriptor.channel_name] = _UnavailableInteractiveApp(
                channel_name=descriptor.channel_name,
                application=self.gateway_application,
                error=error,
            )
            return

        try:
            dependency_report = descriptor.dependency_probe(configuration)
            if not isinstance(
                dependency_report,
                InteractiveAppDependencyReport,
            ):
                raise TypeError(
                    "dependency probe must return InteractiveAppDependencyReport"
                )
        except Exception as exc:
            dependency_report = InteractiveAppDependencyReport(
                available=False,
                detail=f"Dependency probe failed: {exc}",
            )
        record.dependency_report = dependency_report
        if not dependency_report.available:
            error = RuntimeError(
                dependency_report.failure_message(
                    plugin_id=descriptor.plugin_id,
                    channel_name=descriptor.channel_name,
                )
            )
            record.state = InteractiveAppLifecycleState.DEPENDENCY_UNAVAILABLE
            record.error = str(error)
            self.apps[descriptor.channel_name] = _UnavailableInteractiveApp(
                channel_name=descriptor.channel_name,
                application=self.gateway_application,
                error=error,
            )
            return

        context = InteractiveAppBuildContext(
            configuration=configuration,
            application=self.gateway_application,
            bridge=self.bridge,
            runtime=self.runtime,
        )
        try:
            app = self.registry.create(descriptor, context)
        except Exception as exc:
            record.state = InteractiveAppLifecycleState.FAILED
            record.error = f"Plugin construction failed: {exc}"
            self.apps[descriptor.channel_name] = _UnavailableInteractiveApp(
                channel_name=descriptor.channel_name,
                application=self.gateway_application,
                error=exc,
            )
            return

        record.state = InteractiveAppLifecycleState.REGISTERED
        self.apps[descriptor.channel_name] = app

    @property
    def plugin_health(self) -> tuple[InteractiveAppHealth, ...]:
        """Return immutable health snapshots in registration order."""
        return tuple(record.health() for record in self._plugin_records.values())

    def plugin_health_for_channel(
        self, channel_name: str
    ) -> InteractiveAppHealth | None:
        record = self._plugin_records.get(channel_name)
        return record.health() if record is not None else None

    @property
    def enabled_app_names(self) -> list[str]:
        names: list[str] = []
        for name, app in self.apps.items():
            if self.include_apps and name not in self.include_apps:
                continue
            if app.enabled:
                names.append(name)
        return names

    @property
    def has_enabled_apps(self) -> bool:
        return bool(self.enabled_app_names)

    @property
    def running_app_names(self) -> list[str]:
        return [
            name
            for name in self.apps
            if name in self._running_app_names
            and (not self.include_apps or name in self.include_apps)
        ]

    @property
    def has_running_apps(self) -> bool:
        return bool(self.running_app_names)

    async def start(self) -> None:
        for name in self.enabled_app_names:
            record = self._plugin_records.get(name)
            plugin_id = (
                record.descriptor.plugin_id if record is not None else "compatibility"
            )
            if record is not None and not isinstance(
                self.apps[name], _UnavailableInteractiveApp
            ):
                record.state = InteractiveAppLifecycleState.STARTING
                record.error = None
            logger.info(
                "Starting interactive app. event=interactive_app_starting "
                "plugin_id=%s channel_name=%s",
                plugin_id,
                name,
            )
            try:
                await self.apps[name].start()
            except Exception as exc:
                if record is not None:
                    record.state = InteractiveAppLifecycleState.FAILED
                    record.error = str(exc)
                logger.warning(
                    "Failed to start interactive app %s. event=interactive_app_start_failed "
                    "plugin_id=%s channel_name=%s app_name=%s strict_startup=%s",
                    name,
                    plugin_id,
                    name,
                    name,
                    self.strict_startup,
                    exc_info=True,
                )
                try:
                    await self.apps[name].stop()
                except Exception:
                    logger.warning(
                        "Failed to clean up interactive app %s after startup failure.",
                        name,
                        exc_info=True,
                    )
                if self.strict_startup:
                    raise
                continue
            self._running_app_names.add(name)
            if record is not None:
                record.state = InteractiveAppLifecycleState.RUNNING
                record.error = None

    async def stop(self) -> None:
        for name in reversed(self.running_app_names):
            record = self._plugin_records.get(name)
            if record is not None:
                record.state = InteractiveAppLifecycleState.STOPPING
            try:
                await self.apps[name].stop()
            except Exception as exc:
                if record is not None:
                    record.state = InteractiveAppLifecycleState.FAILED
                    record.error = str(exc)
                logger.warning("Failed to stop interactive app %s: %s", name, exc)
            finally:
                self._running_app_names.discard(name)
                if (
                    record is not None
                    and record.state != InteractiveAppLifecycleState.FAILED
                ):
                    record.state = InteractiveAppLifecycleState.STOPPED

    async def handle_manager_unavailable(self) -> None:
        for name in self.running_app_names:
            app = self.apps[name]
            if app.pauses_inbound_when_manager_unavailable():
                logger.info(
                    "Pausing inbound delivery for interactive app %s while manager is unavailable.",
                    name,
                )
                await app.pause_inbound()

    async def handle_manager_available(self) -> None:
        for name in self.running_app_names:
            app = self.apps[name]
            if app.pauses_inbound_when_manager_unavailable():
                logger.info(
                    "Resuming inbound delivery for interactive app %s after manager recovery.",
                    name,
                )
                await app.resume_inbound()

    async def handle_gateway_event(self, event: GatewayDeliveryEvent) -> None:
        channel_name = event.channel_name
        if channel_name and channel_name in self.apps:
            app = self.apps[channel_name]
            if (
                (not self.include_apps or channel_name in self.include_apps)
                and app.enabled
                and channel_name in self._running_app_names
            ):
                try:
                    await app.deliver_event(event)
                    return
                except RuntimeError as exc:
                    if "not running" not in str(exc).lower():
                        raise
                    logger.warning(
                        "Interactive app %s could not deliver gateway event because it is not running; "
                        "falling back to local output.",
                        channel_name,
                    )
            elif (
                not self.include_apps or channel_name in self.include_apps
            ) and app.enabled:
                logger.warning(
                    "Interactive app %s is enabled but not running; falling back to local output.",
                    channel_name,
                )
        await self._emit_fallback(event.text_with_attachment_paths())

    async def _emit_fallback(self, text: str) -> None:
        if self.output_sink is None:
            logger.info("%s", text)
            return
        result = self.output_sink(text)
        if inspect.isawaitable(result):
            await result
