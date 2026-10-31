"""NATS implementation of the gateway manager runtime port."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

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
    ) -> None:
        self.settings = settings
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
    ) -> "NatsGatewayManagerRuntime":
        """Build the runtime from the composition root's resolved config."""

        return cls(
            settings=NatsGatewayManagerRuntimeSettings.from_gateway_config(
                config
            ),
            adapter=adapter,
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
        on_presence: GatewayManagerEnvelopeHandler,
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
            await self._adapter.subscribe_subject(
                name=f"{self.settings.instance_name}:presence",
                subject=f"{self._mapper.prefix}.presence.*",
                handler=self._delivery_handler(on_presence),
                durable_name=self._mapper.durable_name(
                    f"gateway_presence_{response_route}"
                ),
                logical_route=RoutingType.ALL_AGENTS_ROUTE.value,
                consumer_role="gateway_presence",
                deliver_last_per_subject=True,
            )
        except Exception:
            await self._adapter.close()
            raise
        self._connected = True

    async def close(self) -> None:
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

    async def manager_has_live_consumer(
        self,
        manager_id: str,
    ) -> bool | None:
        # The manager's direct mailbox is a pull consumer: missing means the
        # manager is gone, an open request means it is listening, otherwise
        # unknown (it may be handling a message) and presence decides.
        return await self._adapter.consumer_is_listening(
            stream_name=self._mapper.work_stream_name(),
            durable_name=self._mapper.durable_name(f"agent_{manager_id}"),
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
