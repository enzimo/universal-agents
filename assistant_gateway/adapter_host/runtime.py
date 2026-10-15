"""Separately supervised Python runtime for one or more adapter plugins."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone

from assistant_gateway.adapter_host.client_state import (
    AdapterClientDeliveryRecord,
    AdapterClientRegistrationState,
    AdapterClientStateConflict,
    AdapterHostClientStateStore,
)
from assistant_gateway.adapter_host.service import canonical_configuration_digest
from assistant_gateway.adapter_host.transport import (
    AdapterHostClient,
    AdapterHostTransportError,
)
from runtime_settings.interactive_apps import AdapterHostSettings
from assistant_gateway.contracts.adapter_host_v1 import (
    ADAPTER_HOST_PROTOCOL_VERSION,
    AdapterCapabilityV1,
    AdapterAttachmentUploadRequestV1,
    AdapterAttachmentV1,
    AdapterDeliveryAcknowledgementV1,
    AdapterHealthStateV1,
    AdapterHeartbeatV1,
    AdapterHostMessageV1,
    AdapterProtocolLimitsV1,
    AdapterRegistrationV1,
    AdapterResumePositionV1,
    ChannelCommandV1,
    ChannelInboundMessageV1,
    ChannelRequestAcceptedV1,
    DeliveryAcknowledgementStageV1,
    DeliveryOutcomeV1,
    GatewayDeliveryEventV1,
    GatewayRequestStateV1,
    decode_adapter_host_message_v1,
    encode_adapter_host_message_v1,
)
from id_generation import new_id


LOGGER = logging.getLogger(__name__)


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


@dataclass(frozen=True)
class AdapterDeliveryResult:
    outcome: DeliveryOutcomeV1
    provider_message_id: str | None = None
    error_code: str | None = None
    detail: str | None = None


class AmbiguousAdapterDeliveryError(RuntimeError):
    """Platform request began but its externally visible outcome is unknowable."""


DeliveryHandler = Callable[[GatewayDeliveryEventV1], Awaitable[AdapterDeliveryResult]]
RequestStateHandler = Callable[[GatewayRequestStateV1], Awaitable[None]]
LifecycleHandler = Callable[[], Awaitable[None]]


async def _noop_lifecycle() -> None:
    return None


async def _noop_request_state(_event: GatewayRequestStateV1) -> None:
    return None


@dataclass(frozen=True)
class HostedAdapterSpec:
    """Validated plugin and explicit callbacks owned by the Python host."""

    plugin_id: str
    adapter_instance_id: str
    plugin_api_version: str
    implementation_version: str
    channel_names: tuple[str, ...]
    capabilities: tuple[AdapterCapabilityV1, ...]
    configuration: object
    deliver_event: DeliveryHandler
    handle_request_state: RequestStateHandler = _noop_request_state
    start: LifecycleHandler = _noop_lifecycle
    stop: LifecycleHandler = _noop_lifecycle


@dataclass
class _RunningAdapter:
    spec: HostedAdapterSpec
    epoch: str | None = None
    limits: AdapterProtocolLimitsV1 | None = None
    task: asyncio.Task[None] | None = None
    lifecycle_started: bool = False


class AdapterHostRuntime:
    """Reconnect, resume, flow control, and delivery-idempotency coordinator."""

    def __init__(
        self,
        client: AdapterHostClient,
        state: AdapterHostClientStateStore,
        settings: AdapterHostSettings,
        adapters: Sequence[HostedAdapterSpec],
    ) -> None:
        self._client = client
        self._state = state
        self._settings = settings
        self._adapters = {
            spec.plugin_id: _RunningAdapter(spec=spec) for spec in adapters
        }
        if len(self._adapters) != len(adapters):
            raise ValueError("Hosted adapter plugin ids must be unique.")
        self._stopping = asyncio.Event()

    async def start(self) -> None:
        self._stopping.clear()
        await self._client.start()
        recovered = self._state.recover_interrupted_deliveries(
            observed_at_utc=utc_now().isoformat()
        )
        if recovered:
            LOGGER.error(
                "Recovered ambiguous external deliveries without retrying them. "
                "event=adapter_host_external_outcome_unknown count=%d",
                recovered,
            )
        for adapter in self._adapters.values():
            self._ensure_local_registration_state(adapter)
            adapter.task = asyncio.create_task(
                self._run_adapter(adapter),
                name=f"adapter_host_{adapter.spec.plugin_id}",
            )

    def _ensure_local_registration_state(self, adapter: _RunningAdapter) -> None:
        spec = adapter.spec
        if self._state.registration_state(spec.plugin_id, spec.adapter_instance_id):
            return
        self._state.save_registration(
            plugin_id=spec.plugin_id,
            adapter_instance_id=spec.adapter_instance_id,
            registration_epoch="unregistered",
            inbound_committed_position=0,
            outbound_received_position=0,
            outbound_completed_position=0,
            updated_at_utc=utc_now().isoformat(),
        )

    async def stop(self) -> None:
        self._stopping.set()
        tasks = [adapter.task for adapter in self._adapters.values() if adapter.task]
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        for adapter in reversed(tuple(self._adapters.values())):
            if not adapter.lifecycle_started:
                adapter.task = None
                adapter.epoch = None
                continue
            try:
                await adapter.spec.stop()
            except Exception:
                LOGGER.warning(
                    "Hosted adapter stop failed. event=adapter_host_plugin_stop_failed "
                    "plugin_id=%s adapter_instance_id=%s",
                    adapter.spec.plugin_id,
                    adapter.spec.adapter_instance_id,
                    exc_info=True,
                )
            adapter.task = None
            adapter.epoch = None
            adapter.lifecycle_started = False
        await self._client.close()

    async def _run_adapter(self, adapter: _RunningAdapter) -> None:
        delay = self._settings.reconnect_initial_delay_seconds
        while not self._stopping.is_set():
            try:
                if adapter.epoch is None:
                    await self._register(adapter)
                if not adapter.lifecycle_started:
                    # Registration establishes a durable epoch before a
                    # polling/webhook plugin is allowed to accept input.
                    try:
                        await adapter.spec.start()
                    except Exception:
                        try:
                            await adapter.spec.stop()
                        except Exception:
                            LOGGER.warning(
                                "Hosted adapter cleanup after startup failure failed. "
                                "event=adapter_host_plugin_start_cleanup_failed "
                                "plugin_id=%s adapter_instance_id=%s",
                                adapter.spec.plugin_id,
                                adapter.spec.adapter_instance_id,
                                exc_info=True,
                            )
                        raise
                    adapter.lifecycle_started = True
                await self._flush_pending_inbound(adapter)
                await self._flush_acknowledgements(adapter)
                await self._process_locally_pending_deliveries(adapter)
                await self._connected_loop(adapter)
                delay = self._settings.reconnect_initial_delay_seconds
            except asyncio.CancelledError:
                raise
            except Exception:
                LOGGER.warning(
                    "Adapter-host connection lost; bounded reconnect scheduled. "
                    "event=adapter_host_reconnect plugin_id=%s adapter_instance_id=%s "
                    "delay_seconds=%.3f",
                    adapter.spec.plugin_id,
                    adapter.spec.adapter_instance_id,
                    delay,
                    exc_info=True,
                )
                adapter.epoch = None
                try:
                    await asyncio.wait_for(self._stopping.wait(), timeout=delay)
                except TimeoutError:
                    pass
                delay = min(
                    self._settings.reconnect_max_delay_seconds,
                    max(delay * 2, self._settings.reconnect_initial_delay_seconds),
                )

    def _offered_limits(self) -> AdapterProtocolLimitsV1:
        configured = self._settings.limits
        return AdapterProtocolLimitsV1.model_validate(
            configured.model_dump(mode="json")
        )

    async def _register(self, adapter: _RunningAdapter) -> None:
        spec = adapter.spec
        local = self._state.registration_state(spec.plugin_id, spec.adapter_instance_id)
        if local is None:
            self._ensure_local_registration_state(adapter)
            local = self._state.registration_state(
                spec.plugin_id, spec.adapter_instance_id
            )
        if local is None:  # pragma: no cover - local repository invariant
            raise RuntimeError("Adapter client registration state is missing.")
        request = AdapterRegistrationV1(
            protocol_version=ADAPTER_HOST_PROTOCOL_VERSION,
            message_type="adapter.registration.v1",
            registration_id=new_id("adapter_reg"),
            idempotency_key=new_id("adapter_regidem"),
            plugin_id=spec.plugin_id,
            adapter_instance_id=spec.adapter_instance_id,
            plugin_api_version=spec.plugin_api_version,
            implementation_version=spec.implementation_version,
            configuration_digest=canonical_configuration_digest(spec.configuration),
            channel_names=spec.channel_names,
            capabilities=spec.capabilities,
            supported_limits=self._offered_limits(),
            resume=AdapterResumePositionV1(
                inbound_committed_position=local.inbound_committed_position,
                outbound_received_position=local.outbound_received_position,
                outbound_completed_position=local.outbound_completed_position,
            ),
            registered_at_utc=utc_now(),
        )
        accepted = await self._client.register(request)
        if (
            not accepted.accepted
            or not accepted.session_epoch
            or not accepted.negotiated_limits
        ):
            raise AdapterHostTransportError(
                accepted.error_code or "registration_rejected",
                accepted.detail or "adapter registration was rejected",
            )
        adapter.epoch = accepted.session_epoch
        adapter.limits = accepted.negotiated_limits
        now = utc_now().isoformat()
        completed = max(
            local.outbound_completed_position,
            accepted.resume.outbound_completed_position,
        )
        received = max(local.outbound_received_position, completed)
        self._state.save_registration(
            plugin_id=spec.plugin_id,
            adapter_instance_id=spec.adapter_instance_id,
            registration_epoch=accepted.session_epoch,
            inbound_committed_position=local.inbound_committed_position,
            outbound_received_position=received,
            outbound_completed_position=completed,
            updated_at_utc=now,
        )
        self._state.reconcile_inbound_cursor(
            plugin_id=spec.plugin_id,
            adapter_instance_id=spec.adapter_instance_id,
            gateway_committed_position=accepted.resume.inbound_committed_position,
            updated_at_utc=now,
        )
        self._state.reset_epoch_acknowledgements(
            plugin_id=spec.plugin_id,
            adapter_instance_id=spec.adapter_instance_id,
            updated_at_utc=now,
        )
        LOGGER.info(
            "Adapter-host plugin registration ready. event=adapter_host_plugin_ready "
            "plugin_id=%s adapter_instance_id=%s epoch=%s max_in_flight=%d",
            spec.plugin_id,
            spec.adapter_instance_id,
            adapter.epoch,
            adapter.limits.max_in_flight_events,
        )

    async def _connected_loop(self, adapter: _RunningAdapter) -> None:
        if adapter.epoch is None or adapter.limits is None:
            raise RuntimeError(
                "Adapter must be registered before entering the event loop."
            )
        heartbeat_at = asyncio.get_running_loop().time()
        while not self._stopping.is_set():
            await self._flush_pending_inbound(adapter)
            await self._flush_acknowledgements(adapter)
            await self._process_locally_pending_deliveries(adapter)
            state = self._require_local_state(adapter)
            events = await self._client.poll_events(
                plugin_id=adapter.spec.plugin_id,
                adapter_instance_id=adapter.spec.adapter_instance_id,
                session_epoch=adapter.epoch,
                after_position=state.outbound_received_position,
                limit=adapter.limits.max_in_flight_events,
                wait_seconds=min(20.0, adapter.limits.heartbeat_interval_seconds / 2),
            )
            for event in events:
                await self._receive_event(adapter, event)
            now_monotonic = asyncio.get_running_loop().time()
            if now_monotonic >= heartbeat_at:
                await self._send_heartbeat(adapter)
                heartbeat_at = (
                    now_monotonic + adapter.limits.heartbeat_interval_seconds / 2
                )

    def _require_local_state(
        self, adapter: _RunningAdapter
    ) -> AdapterClientRegistrationState:
        state = self._state.registration_state(
            adapter.spec.plugin_id, adapter.spec.adapter_instance_id
        )
        if state is None:
            raise RuntimeError("Adapter client registration state is missing.")
        return state

    async def _receive_event(
        self, adapter: _RunningAdapter, event: AdapterHostMessageV1
    ) -> None:
        if adapter.epoch is None:
            raise RuntimeError("Adapter registration epoch is missing.")
        if not isinstance(event, (GatewayDeliveryEventV1, GatewayRequestStateV1)):
            raise AdapterHostTransportError(
                "unexpected_message_type",
                f"event stream returned {type(event).__name__}",
            )
        record = self._state.receive_delivery(
            event_id=event.event_id,
            plugin_id=event.plugin_id,
            adapter_instance_id=adapter.spec.adapter_instance_id,
            registration_epoch=adapter.epoch,
            stream_position=event.stream_position,
            payload=encode_adapter_host_message_v1(event),
            observed_at_utc=utc_now().isoformat(),
        )
        await self._ack_receipt(adapter, record)
        if record.status == "received":
            await self._deliver_record(adapter, record)

    async def _process_locally_pending_deliveries(
        self, adapter: _RunningAdapter
    ) -> None:
        for record in self._state.pending_deliveries(
            plugin_id=adapter.spec.plugin_id,
            adapter_instance_id=adapter.spec.adapter_instance_id,
        ):
            await self._ack_receipt(adapter, record)
            await self._deliver_record(adapter, record)

    async def _deliver_record(
        self, adapter: _RunningAdapter, record: AdapterClientDeliveryRecord
    ) -> None:
        if not self._state.begin_delivery(
            record.event_id, observed_at_utc=utc_now().isoformat()
        ):
            return
        message = decode_adapter_host_message_v1(json.loads(record.payload_json))
        try:
            if isinstance(message, GatewayDeliveryEventV1):
                result = await adapter.spec.deliver_event(message)
            elif isinstance(message, GatewayRequestStateV1):
                await adapter.spec.handle_request_state(message)
                result = AdapterDeliveryResult(outcome=DeliveryOutcomeV1.DELIVERED)
            else:  # pragma: no cover - local database corruption guard
                raise TypeError(
                    f"Unsupported delivery record {type(message).__name__}."
                )
        except AmbiguousAdapterDeliveryError as exc:
            result = AdapterDeliveryResult(
                outcome=DeliveryOutcomeV1.OUTCOME_UNKNOWN,
                error_code="external_delivery_outcome_unknown",
                detail=str(exc)[:2_000],
            )
        except Exception as exc:
            result = AdapterDeliveryResult(
                outcome=DeliveryOutcomeV1.FAILED,
                error_code="external_delivery_failed",
                detail=str(exc)[:2_000],
            )
            LOGGER.warning(
                "External adapter delivery failed definitively. "
                "event=adapter_host_delivery_failed plugin_id=%s adapter_instance_id=%s "
                "event_id=%s stream_position=%d",
                adapter.spec.plugin_id,
                adapter.spec.adapter_instance_id,
                record.event_id,
                record.stream_position,
                exc_info=True,
            )
        completed = self._state.complete_delivery(
            record.event_id,
            outcome=str(result.outcome),
            provider_message_id=result.provider_message_id,
            error_code=result.error_code,
            detail=result.detail,
            observed_at_utc=utc_now().isoformat(),
        )
        await self._ack_completion(adapter, completed)

    async def _flush_acknowledgements(self, adapter: _RunningAdapter) -> None:
        for record in self._state.pending_acknowledgements(
            plugin_id=adapter.spec.plugin_id,
            adapter_instance_id=adapter.spec.adapter_instance_id,
        ):
            if not record.receipt_acknowledged:
                await self._ack_receipt(adapter, record)
            if record.outcome is not None and not record.completion_acknowledged:
                await self._ack_completion(adapter, record)

    async def _ack_receipt(
        self, adapter: _RunningAdapter, record: AdapterClientDeliveryRecord
    ) -> None:
        epoch = adapter.epoch
        if epoch is None:
            raise RuntimeError("Cannot acknowledge without a registration epoch.")
        now = utc_now()
        await self._client.acknowledge(
            AdapterDeliveryAcknowledgementV1(
                protocol_version=ADAPTER_HOST_PROTOCOL_VERSION,
                message_type="adapter.delivery_ack.v1",
                acknowledgement_id=new_id("adapter_ack"),
                idempotency_key=f"receipt:{record.event_id}:{epoch}",
                plugin_id=record.plugin_id,
                adapter_instance_id=record.adapter_instance_id,
                session_epoch=epoch,
                event_id=record.event_id,
                stream_position=record.stream_position,
                stage=DeliveryAcknowledgementStageV1.RECEIVED,
                observed_at_utc=now,
            )
        )
        self._state.mark_receipt_acknowledged(
            record.event_id, observed_at_utc=now.isoformat()
        )

    async def _ack_completion(
        self, adapter: _RunningAdapter, record: AdapterClientDeliveryRecord
    ) -> None:
        epoch = adapter.epoch
        if epoch is None or record.outcome is None:
            raise RuntimeError(
                "Cannot acknowledge completion without epoch and outcome."
            )
        now = utc_now()
        await self._client.acknowledge(
            AdapterDeliveryAcknowledgementV1(
                protocol_version=ADAPTER_HOST_PROTOCOL_VERSION,
                message_type="adapter.delivery_ack.v1",
                acknowledgement_id=new_id("adapter_ack"),
                idempotency_key=f"completion:{record.event_id}:{epoch}",
                plugin_id=record.plugin_id,
                adapter_instance_id=record.adapter_instance_id,
                session_epoch=epoch,
                event_id=record.event_id,
                stream_position=record.stream_position,
                stage=DeliveryAcknowledgementStageV1.COMPLETED,
                outcome=DeliveryOutcomeV1(record.outcome),
                provider_message_id=record.provider_message_id,
                observed_at_utc=now,
                error_code=record.error_code,
                detail=record.detail,
            )
        )
        self._state.mark_completion_acknowledged(
            record.event_id, observed_at_utc=now.isoformat()
        )

    async def _send_heartbeat(self, adapter: _RunningAdapter) -> None:
        epoch = adapter.epoch
        if epoch is None:
            raise RuntimeError("Cannot heartbeat without a registration epoch.")
        state = self._require_local_state(adapter)
        pending = self._state.pending_deliveries(
            plugin_id=adapter.spec.plugin_id,
            adapter_instance_id=adapter.spec.adapter_instance_id,
        )
        await self._client.heartbeat(
            AdapterHeartbeatV1(
                protocol_version=ADAPTER_HOST_PROTOCOL_VERSION,
                message_type="adapter.heartbeat.v1",
                heartbeat_id=new_id("adapter_hb"),
                idempotency_key=new_id("adapter_hbidem"),
                plugin_id=adapter.spec.plugin_id,
                adapter_instance_id=adapter.spec.adapter_instance_id,
                session_epoch=epoch,
                state=AdapterHealthStateV1.READY,
                inbound_committed_position=state.inbound_committed_position,
                outbound_received_position=state.outbound_received_position,
                outbound_completed_position=state.outbound_completed_position,
                in_flight_events=max(
                    0,
                    state.outbound_received_position
                    - state.outbound_completed_position,
                ),
                local_queue_depth=len(pending),
                observed_at_utc=utc_now(),
            )
        )

    async def submit(
        self, message: ChannelInboundMessageV1 | ChannelCommandV1
    ) -> ChannelRequestAcceptedV1 | None:
        adapter = self._adapters.get(message.plugin_id)
        if adapter is None:
            raise AdapterHostTransportError(
                "adapter_not_configured",
                "adapter is not configured in this host",
            )
        self._ensure_local_registration_state(adapter)
        local = self._require_local_state(adapter)
        if local.registration_epoch is None:  # pragma: no cover - state invariant
            raise RuntimeError("Adapter client registration epoch is missing.")
        now = utc_now()
        logical = message.model_copy(
            update={"session_epoch": "pending", "stream_position": 1}
        )
        record = self._state.queue_inbound(
            request_id=message.request_id,
            idempotency_key=message.idempotency_key,
            plugin_id=message.plugin_id,
            adapter_instance_id=message.adapter_instance_id,
            registration_epoch=local.registration_epoch,
            payload=encode_adapter_host_message_v1(logical),
            updated_at_utc=now.isoformat(),
        )
        if adapter.epoch is None:
            LOGGER.warning(
                "Adapter input durably queued while the gateway connection is unavailable. "
                "event=adapter_host_input_queued_offline plugin_id=%s "
                "adapter_instance_id=%s request_id=%s stream_position=%d",
                adapter.spec.plugin_id,
                adapter.spec.adapter_instance_id,
                record.request_id,
                record.stream_position,
            )
            return None
        wire_message = logical.model_copy(
            update={
                "session_epoch": adapter.epoch,
                "stream_position": record.stream_position,
            }
        )
        accepted = await self._client.submit(wire_message)
        self._state.mark_inbound_accepted(
            record.request_id,
            correlation_id=accepted.correlation_id,
            updated_at_utc=utc_now().isoformat(),
        )
        return accepted

    def registration_epoch(self, plugin_id: str) -> str | None:
        adapter = self._adapters.get(plugin_id)
        return adapter.epoch if adapter is not None else None

    async def upload_attachment(
        self,
        *,
        plugin_id: str,
        adapter_instance_id: str,
        filename: str,
        content_type: str,
        kind: str,
        payload: bytes,
        attachment_id: str,
    ) -> AdapterAttachmentV1:
        adapter = self._adapters.get(plugin_id)
        if (
            adapter is None
            or adapter.epoch is None
            or adapter.spec.adapter_instance_id != adapter_instance_id
        ):
            raise AdapterHostTransportError(
                "adapter_not_ready",
                "adapter is not registered with the gateway",
                retryable=True,
            )
        digest = hashlib.sha256(payload).hexdigest()
        request = AdapterAttachmentUploadRequestV1(
            protocol_version=ADAPTER_HOST_PROTOCOL_VERSION,
            message_type="adapter.attachment_upload_request.v1",
            upload_request_id=new_id("adapter_upreq"),
            idempotency_key=f"upload:{attachment_id}:{digest}",
            plugin_id=plugin_id,
            adapter_instance_id=adapter_instance_id,
            session_epoch=adapter.epoch,
            filename=filename,
            content_type=content_type,
            kind=kind,
            size_bytes=len(payload),
            sha256=digest,
            requested_at_utc=utc_now(),
        )
        ticket = await self._client.create_upload_ticket(request)
        await self._client.upload(ticket, payload)
        return AdapterAttachmentV1(
            attachment_id=attachment_id,
            filename=filename,
            content_type=content_type,
            kind=kind,
            size_bytes=len(payload),
            sha256=digest,
            storage_reference=ticket.storage_reference,
        )

    async def download_attachment(
        self,
        *,
        plugin_id: str,
        adapter_instance_id: str,
        attachment: AdapterAttachmentV1,
    ) -> bytes:
        adapter = self._adapters.get(plugin_id)
        if (
            adapter is None
            or adapter.epoch is None
            or adapter.spec.adapter_instance_id != adapter_instance_id
        ):
            raise AdapterHostTransportError(
                "adapter_not_ready",
                "adapter is not registered with the gateway",
                retryable=True,
            )
        return await self._client.download_attachment(
            plugin_id=plugin_id,
            adapter_instance_id=adapter_instance_id,
            session_epoch=adapter.epoch,
            storage_reference=attachment.storage_reference,
            expected_size_bytes=attachment.size_bytes,
            expected_sha256=attachment.sha256,
        )

    async def _flush_pending_inbound(self, adapter: _RunningAdapter) -> None:
        epoch = adapter.epoch
        if epoch is None:
            return
        for record in self._state.pending_inbound(
            plugin_id=adapter.spec.plugin_id,
            adapter_instance_id=adapter.spec.adapter_instance_id,
        ):
            message = decode_adapter_host_message_v1(json.loads(record.payload_json))
            if not isinstance(message, (ChannelInboundMessageV1, ChannelCommandV1)):
                raise AdapterClientStateConflict(
                    "client_inbox_corruption",
                    "client inbox contains an unsupported protocol message",
                )
            accepted = await self._client.submit(
                message.model_copy(
                    update={
                        "session_epoch": epoch,
                        "stream_position": record.stream_position,
                    }
                )
            )
            self._state.mark_inbound_accepted(
                record.request_id,
                correlation_id=accepted.correlation_id,
                updated_at_utc=utc_now().isoformat(),
            )


__all__ = [
    "AdapterDeliveryResult",
    "AdapterHostRuntime",
    "AmbiguousAdapterDeliveryError",
    "HostedAdapterSpec",
]
