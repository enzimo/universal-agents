"""Gateway composition root for the authenticated external adapter host."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path

from assistant_gateway.adapter_host.gateway_application import (
    GatewayAdapterRequestHandler,
    gateway_delivery_to_wire,
)
from assistant_gateway.adapter_host.service import (
    AdapterHostApplicationService,
    AdapterHostPolicy,
    AdapterPluginPolicy,
    canonical_configuration_digest,
)
from assistant_gateway.adapter_host.transport import AdapterHostServer
from assistant_gateway.application.gateway_application import (
    GatewayApplication,
    GatewayDeliveryEvent,
)
from assistant_gateway.attachments import gateway_staged_attachment_reader
from runtime_settings.interactive_apps import INTERACTIVE_APP_EXECUTION_ADAPTER_HOST
from assistant_gateway.config import GatewayInstanceConfig
from assistant_gateway.contracts.adapter_host_v1 import (
    AdapterAttachmentV1,
    AdapterCapabilityV1,
    AdapterProtocolLimitsV1,
)
from assistant_gateway.interactive_apps.builtin_registry import (
    build_interactive_app_registry,
)
from assistant_gateway.interactive_apps.registry import InteractiveAppRegistry
from storage.adapter_host_repository import create_adapter_host_repository
from storage.config import StorageSettings


LOGGER = logging.getLogger(__name__)


def _resolve_instance_path(instance_root: Path, raw_path: str) -> Path:
    path = Path(raw_path).expanduser()
    return path.resolve() if path.is_absolute() else (instance_root / path).resolve()


@dataclass(frozen=True)
class ExternalAdapterRoute:
    """Operator-owned channel-to-plugin delivery route."""

    plugin_id: str
    adapter_instance_id: str
    channel_name: str


def build_adapter_host_policy(
    config: GatewayInstanceConfig,
    registry: InteractiveAppRegistry,
) -> tuple[AdapterHostPolicy, tuple[ExternalAdapterRoute, ...]]:
    """Project exact enabled external plugins into registration policy."""

    settings = config.interactive_apps.adapter_host
    plugins: dict[str, AdapterPluginPolicy] = {}
    routes: list[ExternalAdapterRoute] = []
    for descriptor in registry.descriptors:
        configured = config.interactive_apps.require_plugin(descriptor.plugin_id)
        if (
            not configured.enabled
            or configured.execution != INTERACTIVE_APP_EXECUTION_ADAPTER_HOST
        ):
            continue
        validated = descriptor.validate_configuration(configured.settings)
        adapter_instance_id = config.interactive_apps.resolved_adapter_instance_id(
            descriptor.plugin_id, config.instance_name
        )
        policy = AdapterPluginPolicy(
            plugin_id=descriptor.plugin_id,
            adapter_instance_id=adapter_instance_id,
            allowed_host_ids=frozenset(settings.allowed_host_ids),
            configuration_digest=canonical_configuration_digest(validated),
            plugin_api_version=descriptor.plugin_api_version,
            channel_names=frozenset({descriptor.channel_name}),
            capabilities=frozenset(
                AdapterCapabilityV1(capability.value)
                for capability in descriptor.capabilities
            ),
        )
        plugins[descriptor.plugin_id] = policy
        routes.append(
            ExternalAdapterRoute(
                plugin_id=descriptor.plugin_id,
                adapter_instance_id=adapter_instance_id,
                channel_name=descriptor.channel_name,
            )
        )
    if settings.enabled and not plugins:
        raise ValueError(
            "interactive_apps.adapter_host.enabled requires at least one enabled "
            "plugin with execution=adapter_host."
        )
    limits = AdapterProtocolLimitsV1.model_validate(
        settings.limits.model_dump(mode="json")
    )
    return (
        AdapterHostPolicy(
            gateway_instance_id=config.instance_name,
            plugins=plugins,
            limits=limits,
            lease_seconds=settings.lease_seconds,
            upload_ticket_ttl_seconds=settings.upload_ticket_ttl_seconds,
            upload_dir=_resolve_instance_path(
                config.instance_root_path, settings.upload_dir
            ),
        ),
        tuple(routes),
    )


class GatewayAdapterHostRuntime:
    """Own the gateway server and route external-channel delivery durably."""

    def __init__(
        self,
        config: GatewayInstanceConfig,
        application: GatewayApplication,
        storage_settings: StorageSettings,
        *,
        registry: InteractiveAppRegistry | None = None,
    ) -> None:
        self._config = config
        self._outgoing_attachments = gateway_staged_attachment_reader(config)
        self._registry = registry or build_interactive_app_registry(config)
        policy, routes = build_adapter_host_policy(config, self._registry)
        self._routes = {route.channel_name: route for route in routes}
        repository = create_adapter_host_repository(
            storage_settings,
            logger=LOGGER,
        )
        holder: dict[str, AdapterHostApplicationService] = {}
        handler = GatewayAdapterRequestHandler(
            application,
            lambda: holder["service"],
        )
        self.service = AdapterHostApplicationService(
            repository,
            policy,
            request_handler=handler,
        )
        holder["service"] = self.service
        self.server = AdapterHostServer(
            self.service,
            config.interactive_apps.adapter_host,
            instance_root=config.instance_root_path,
        )

    @property
    def channel_names(self) -> tuple[str, ...]:
        return tuple(self._routes)

    @property
    def has_routes(self) -> bool:
        return bool(self._routes)

    def owns_channel(self, channel_name: str | None) -> bool:
        return bool(channel_name and channel_name in self._routes)

    async def start(self) -> None:
        await self.server.start()

    async def stop(self) -> None:
        await self.server.stop()

    async def handle_gateway_event(self, event: GatewayDeliveryEvent) -> bool:
        route = self._routes.get(event.channel_name or "")
        if route is None:
            return False
        attachments: list[AdapterAttachmentV1] = []
        # The transfer store receives the checked bytes of the gateway's staged
        # copy, never a path; a copy that changed after staging is skipped.
        for attachment, payload in self._outgoing_attachments.readable(
            event.attachments
        ):
            attachments.append(
                self.service.stage_outbound_attachment(
                    plugin_id=route.plugin_id,
                    adapter_instance_id=route.adapter_instance_id,
                    payload=payload,
                    filename=attachment.filename or Path(attachment.path).name,
                    content_type=(
                        attachment.content_type or "application/octet-stream"
                    ),
                    kind=attachment.kind,
                )
            )
        position = await self.service.enqueue_event(
            gateway_delivery_to_wire(
                event,
                plugin_id=route.plugin_id,
                adapter_instance_id=route.adapter_instance_id,
                attachments=tuple(attachments),
            )
        )
        LOGGER.info(
            "Gateway delivery committed to external adapter outbox. "
            "event=adapter_host_delivery_enqueued plugin_id=%s "
            "adapter_instance_id=%s event_id=%s stream_position=%d",
            route.plugin_id,
            route.adapter_instance_id,
            event.event_id,
            position,
        )
        return True

    def health(self) -> tuple[dict[str, object], ...]:
        return tuple(
            self.service.health(route.plugin_id, route.adapter_instance_id)
            for route in self._routes.values()
        )


__all__ = [
    "ExternalAdapterRoute",
    "GatewayAdapterHostRuntime",
    "build_adapter_host_policy",
]
