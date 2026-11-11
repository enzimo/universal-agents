"""NATS implementation of the gateway manager runtime port."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from assistant_gateway.application.ports import GatewayManagerEnvelopeHandler
from assistant_gateway.config import GatewayInstanceConfig
from custom_messages.contracts.envelope_v1 import MessageEnvelope
from custom_messages.enums import RoutingType
from custom_transport.base import (
    DeliveryHandler,
    InboundDelivery,
    SubscriptionDelivery,
    TransportPublishOptions,
)
from custom_transport.nats_adapter import JetStreamAccountUsage, NatsTransportAdapter, NatsTransportConfig
from custom_transport.subjects import NatsSubjectMapper

if TYPE_CHECKING:
    from assistant_gateway.message_host import GatewayMessageHost


@dataclass(frozen=True)
class NatsGatewayManagerRuntimeSettings:
    """NATS settings owned by the concrete manager runtime adapter."""

    instance_name: str
    response_route: str
    team_id: str
    servers: tuple[str, ...]
    name: str
    subject_prefix: str
    jetstream_enabled: bool
    storage: str
    replicas: int
    ack_wait_seconds: float
    max_deliver: int
    duplicate_window_seconds: float
    work_max_age_seconds: float | None
    presence_max_age_seconds: float | None
    process_consumer_inactive_threshold_seconds: float
    user: str | None
    password: str | None
    credentials_file: str | None

    @classmethod
    def from_gateway_config(
        cls,
        config: GatewayInstanceConfig,
    ) -> "NatsGatewayManagerRuntimeSettings":
        transport = config.transport
        servers = tuple(
            transport.nats_servers
            or ([transport.nats_url] if transport.nats_url else [])
        )
        return cls(
            instance_name=config.instance_name,
            response_route=config.manager.response_queue_name,
            team_id=config.manager.team_id,
            servers=servers,
            name=transport.nats_name or f"{config.instance_name}-gateway",
            subject_prefix=transport.subject_prefix,
            jetstream_enabled=transport.jetstream_enabled,
            storage=transport.storage,
            replicas=transport.replicas,
            ack_wait_seconds=transport.ack_wait_seconds,
            max_deliver=transport.max_deliver,
            duplicate_window_seconds=transport.duplicate_window_seconds,
            work_max_age_seconds=transport.work_max_age_seconds,
            presence_max_age_seconds=transport.presence_max_age_seconds,
            process_consumer_inactive_threshold_seconds=(
                transport.process_consumer_inactive_threshold_seconds
            ),
            user=transport.nats_user,
            password=transport.nats_password,
            credentials_file=transport.credentials_file,
        )


class NatsGatewayManagerRuntime:
    """Own gateway NATS connection, subscriptions, publishing, and objects."""

    def __init__(
        self,
        *,
        settings: NatsGatewayManagerRuntimeSettings,
        adapter: NatsTransportAdapter | None = None,
        message_host: "GatewayMessageHost | None" = None,
    ) -> None:
        self.settings = settings
        # The gateway service's protocol 1 host; None in every other process that opens a
        # gateway (assistant_gateway.message_host).
        self.message_host = message_host
        self._mapper = NatsSubjectMapper(
            team_id=settings.team_id,
            subject_prefix=settings.subject_prefix,
            user_queue_name=settings.response_route,
        )
        self._adapter = adapter or NatsTransportAdapter(
            config=NatsTransportConfig(
                servers=list(settings.servers),
                name=settings.name,
                subject_prefix=settings.subject_prefix,
                jetstream_enabled=settings.jetstream_enabled,
                storage=settings.storage,
                replicas=settings.replicas,
                ack_wait_seconds=settings.ack_wait_seconds,
                max_deliver=settings.max_deliver,
                duplicate_window_seconds=settings.duplicate_window_seconds,
                work_max_age_seconds=settings.work_max_age_seconds,
                presence_max_age_seconds=settings.presence_max_age_seconds,
                process_consumer_inactive_threshold_seconds=(
                    settings.process_consumer_inactive_threshold_seconds
                ),
                user=settings.user,
                password=settings.password,
                credentials_file=settings.credentials_file,
                # The host's requests take their replies on its own inbox, which its
                # credential may be limited to (custom_transport.protocol1.live).
                reply_inbox_runtime=message_host.runtime if message_host is not None else None,
            ),
            mapper=self._mapper,
        )
        self._connected = False

    @classmethod
    def from_gateway_config(
        cls,
        config: GatewayInstanceConfig,
        *,
        adapter: NatsTransportAdapter | None = None,
        message_host: "GatewayMessageHost | None" = None,
    ) -> "NatsGatewayManagerRuntime":
        """Build the runtime from the composition root's resolved config."""

        return cls(
            settings=NatsGatewayManagerRuntimeSettings.from_gateway_config(
                config
            ),
            adapter=adapter,
            message_host=message_host,
        )

    @classmethod
    def for_gateway_service(cls, config: GatewayInstanceConfig) -> "NatsGatewayManagerRuntime":
        """The gateway service's runtime, the one process that runs the gateway's protocol 1 host.

        Command-line runs build theirs with ``from_gateway_config`` and run no host
        (assistant_gateway.message_host); nor do they load its code, imported here.
        """

        from assistant_gateway.message_host import prepared_gateway_host

        return cls.from_gateway_config(
            config, message_host=prepared_gateway_host(config.layout, config.manager.team_id)
        )

    @property
    def connected(self) -> bool:
        return self._connected

    async def prepare_topology(self) -> tuple[str, ...]:
        """Create or update the team's streams with these settings, then disconnect.

        The headless launcher runs this before it starts any service
        (``docker.launch_headless.prepare_message_streams``): the manager and
        the agents only check that the streams exist, once, when they
        connect (``custom_transport.nats_topology``), so the streams must
        exist before they start. ``connect`` repeats the same setup, which
        then finds nothing to change. Returns the stream names, or an empty
        tuple without connecting when JetStream is off (there are no streams).
        """

        if self._connected:
            raise RuntimeError("The gateway's streams are set up before it connects.")
        if not self.settings.jetstream_enabled:
            return ()
        await self._adapter.connect(reconcile_topology=True)
        await self._adapter.close()
        return self._adapter.stream_names()

    async def connect(
        self,
        *,
        on_response: GatewayManagerEnvelopeHandler,
        on_operational: GatewayManagerEnvelopeHandler,
    ) -> None:
        if self._connected:
            return
        await self._adapter.connect(reconcile_topology=True)
        try:
            response_route = self.settings.response_route
            await self._adapter.subscribe_route(
                name=f"{self.settings.instance_name}:gateway_response",
                routing_key=response_route,
                handler=self._delivery_handler(on_response),
                durable_name=self._mapper.durable_name(
                    f"gateway_{response_route}"
                ),
                consumer_role="gateway_response",
            )
            await self._adapter.subscribe_subject(
                name=f"{self.settings.instance_name}:operational",
                subject=self._mapper.operational_subject(),
                handler=self._delivery_handler(on_operational),
                logical_route=RoutingType.ALL_AGENTS_ROUTE.value,
                delivery=SubscriptionDelivery.CORE,
            )
        except Exception:
            await self._adapter.close()
            raise
        self._connected = True
        if self.message_host is not None:
            # Joins in the background: the gateway's other work never waits for it.
            self.message_host.start(self._adapter.nats_connection)

    async def close(self) -> None:
        try:
            if self.message_host is not None:
                # Before the connection it runs on; it writes its last host record.
                await self.message_host.close()
        finally:
            await self._adapter.close()
            self._connected = False

    def stopped_receivers(self) -> tuple[Any, ...]:
        return self._adapter.stopped_receivers()

    async def jetstream_usage(self) -> JetStreamAccountUsage | None:
        """The account's stored messages and limits (Status page); None while not connected or without JetStream."""

        if not self._connected or not self.settings.jetstream_enabled:
            return None
        return await self._adapter.jetstream_account_usage()

    @staticmethod
    def _delivery_handler(
        handler: GatewayManagerEnvelopeHandler,
    ) -> DeliveryHandler:
        async def _handle(delivery: InboundDelivery) -> None:
            await handler(delivery.envelope)
            await delivery.ack()

        return _handle

    async def publish_durable(
        self,
        envelope: MessageEnvelope,
        *,
        routing_key: str,
    ) -> None:
        await self._adapter.publish(
            envelope=envelope,
            routing_key=routing_key,
            options=TransportPublishOptions(
                durable=True,
                idempotency_key=(
                    envelope.msg_header.idempotency_key
                    or envelope.msg_header.message_id
                ),
            ),
        )

    async def publish_operational(
        self,
        envelope: MessageEnvelope,
        *,
        routing_key: str,
    ) -> None:
        await self._adapter.publish(
            envelope=envelope,
            routing_key=routing_key,
            options=TransportPublishOptions(
                durable=False,
                operational=True,
                idempotency_key=(
                    envelope.msg_header.idempotency_key
                    or envelope.msg_header.message_id
                ),
            ),
        )

    async def put_object(
        self,
        *,
        bucket: str,
        object_name: str,
        path: str,
        ttl_seconds: float | None = None,
        max_bytes: int | None = None,
        headers: dict[str, str] | None = None,
    ) -> object:
        return await self._adapter.put_object(
            bucket=bucket,
            object_name=object_name,
            path=path,
            ttl_seconds=ttl_seconds,
            max_bytes=max_bytes,
            headers=headers,
        )
