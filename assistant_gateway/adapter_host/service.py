"""Application service for authenticated, durable adapter-host communication."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Protocol

from assistant_gateway.adapter_host.domain import (
    AdapterHostHealthSnapshot,
    AdapterInboxRecord,
    AdapterRegistrationRecord,
    AdapterUploadRecord,
)
from assistant_gateway.adapter_host.repository import AdapterHostRepository
from assistant_gateway.contracts.adapter_host_v1 import (
    ADAPTER_HOST_PROTOCOL_VERSION,
    AdapterAttachmentUploadAcceptedV1,
    AdapterAttachmentUploadRequestV1,
    AdapterAttachmentV1,
    AdapterCapabilityV1,
    AdapterDeliveryAcknowledgementV1,
    AdapterHeartbeatV1,
    AdapterProtocolLimitsV1,
    AdapterRegistrationAcceptedV1,
    AdapterRegistrationV1,
    AdapterResumePositionV1,
    ChannelCommandV1,
    ChannelInboundMessageV1,
    ChannelRequestAcceptedV1,
    ChannelRequestStatusV1,
    GatewayDeliveryEventV1,
    GatewayRequestStateV1,
    decode_adapter_host_message_v1,
    encode_adapter_host_message_v1,
)
from id_generation import new_id
from storage.adapter_host_repository import AdapterHostRepositoryConflict


LOGGER = logging.getLogger(__name__)


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def canonical_configuration_digest(configuration: object) -> str:
    """Return the stable digest used to bind registration to operator settings."""

    model_dump = getattr(configuration, "model_dump", None)
    payload = model_dump(mode="json") if callable(model_dump) else configuration
    rendered = json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    ).encode("utf-8")
    return hashlib.sha256(rendered).hexdigest()


@dataclass(frozen=True)
class AdapterHostAuthenticatedPeer:
    """Identity established by the transport, never by a plugin payload."""

    host_id: str
    authentication_mechanism: str
    credential_fingerprint: str


@dataclass(frozen=True)
class AdapterPluginPolicy:
    """Operator-owned exact registration allowlist for one plugin instance."""

    plugin_id: str
    adapter_instance_id: str
    allowed_host_ids: frozenset[str]
    configuration_digest: str
    plugin_api_version: str
    channel_names: frozenset[str]
    capabilities: frozenset[AdapterCapabilityV1]


@dataclass(frozen=True)
class AdapterHostPolicy:
    """Gateway-owned registration policy and bounded protocol limits."""

    gateway_instance_id: str
    plugins: Mapping[str, AdapterPluginPolicy]
    limits: AdapterProtocolLimitsV1
    lease_seconds: int
    upload_ticket_ttl_seconds: int
    upload_dir: Path


class AdapterHostRequestHandler(Protocol):
    """Typed gateway application operations invoked after durable admission."""

    async def handle_inbound(
        self, message: ChannelInboundMessageV1
    ) -> Sequence[GatewayDeliveryEventV1]: ...

    async def handle_command(
        self, message: ChannelCommandV1
    ) -> Sequence[GatewayDeliveryEventV1]: ...


class AdapterHostServiceError(RuntimeError):
    """Stable fail-closed application error exposed by private transports."""

    def __init__(
        self,
        error_code: str,
        detail: str,
        *,
        http_status: int = 409,
        retryable: bool = False,
        expected_stream_position: int | None = None,
    ) -> None:
        super().__init__(detail)
        self.error_code = error_code
        self.detail = detail
        self.http_status = http_status
        self.retryable = retryable
        self.expected_stream_position = expected_stream_position


class AdapterHostApplicationService:
    """Own registration, leases, inbox/outbox durability, replay, and uploads."""

    def __init__(
        self,
        repository: AdapterHostRepository,
        policy: AdapterHostPolicy,
        *,
        request_handler: AdapterHostRequestHandler | None = None,
        clock: Callable[[], datetime] = utc_now,
    ) -> None:
        self._repository = repository
        self._policy = policy
        self._request_handler = request_handler
        self._clock = clock
        self._tasks: set[asyncio.Task[None]] = set()
        self._event_condition = asyncio.Condition()
        self._started = False

    async def start(self) -> None:
        if self._started:
            return
        self._started = True
        interrupted = self._repository.mark_interrupted_inbox_outcome_unknown()
        if interrupted:
            LOGGER.error(
                "Adapter-host requests had ambiguous gateway outcomes after restart. "
                "event=adapter_host_interrupted_requests_outcome_unknown count=%d",
                interrupted,
            )
        if self._request_handler is not None:
            for record in self._repository.list_inbox_for_recovery(limit=1_000):
                self._schedule_processing(record)
        maintenance = asyncio.create_task(
            self._maintenance_loop(), name="adapter_host_retention"
        )
        self._tasks.add(maintenance)
        maintenance.add_done_callback(self._tasks.discard)

    async def close(self) -> None:
        tasks = tuple(self._tasks)
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._tasks.clear()
        self._started = False

    async def _maintenance_loop(self) -> None:
        """Prune only terminal protocol records after the retention floor.

        The prune runs on a worker thread so a slow or locked database never
        stops the gateway's event loop, which also serves chat connections.
        """

        retention_seconds = self._policy.limits.retention_seconds
        interval_seconds = min(300.0, max(15.0, retention_seconds / 4.0))
        while self._started:
            try:
                removed = await asyncio.to_thread(self._prune_terminal_records)
                if any(removed.values()):
                    LOGGER.info(
                        "Pruned terminal adapter-host records. "
                        "event=adapter_host_retention_pruned inbox=%d outbox=%d uploads=%d",
                        removed["inbox"],
                        removed["outbox"],
                        removed["uploads"],
                    )
            except asyncio.CancelledError:
                raise
            except Exception:
                LOGGER.exception(
                    "Adapter-host retention maintenance failed. "
                    "event=adapter_host_retention_failed"
                )
            await asyncio.sleep(interval_seconds)

    def _prune_terminal_records(self) -> dict[str, int]:
        cutoff = self._clock() - timedelta(
            seconds=self._policy.limits.retention_seconds
        )
        return self._repository.prune(cutoff_utc=cutoff.isoformat())

    def _plugin_policy(self, plugin_id: str) -> AdapterPluginPolicy:
        plugin = self._policy.plugins.get(plugin_id)
        if plugin is None:
            raise AdapterHostServiceError(
                "plugin_not_allowlisted",
                f"plugin {plugin_id!r} is not in the operator-owned adapter-host allowlist",
                http_status=403,
            )
        return plugin

    def _current_registration(
        self,
        *,
        peer: AdapterHostAuthenticatedPeer,
        plugin_id: str,
        adapter_instance_id: str,
        session_epoch: str,
        require_live_lease: bool = True,
    ) -> AdapterRegistrationRecord:
        registration = self._repository.get_registration(plugin_id, adapter_instance_id)
        if registration is None:
            raise AdapterHostServiceError(
                "adapter_not_registered",
                "adapter registration does not exist",
                http_status=403,
            )
        if registration.authenticated_host_id != peer.host_id:
            raise AdapterHostServiceError(
                "authenticated_host_mismatch",
                "adapter registration belongs to a different authenticated host",
                http_status=403,
            )
        if registration.registration_epoch != session_epoch:
            raise AdapterHostServiceError(
                "stale_registration_epoch",
                "registration epoch is stale",
                http_status=409,
            )
        if require_live_lease:
            expires_at = datetime.fromisoformat(registration.lease_expires_at_utc)
            if self._clock() >= expires_at:
                raise AdapterHostServiceError(
                    "registration_lease_expired",
                    "adapter registration lease has expired; register again",
                    http_status=409,
                )
        return registration

    def register(
        self,
        peer: AdapterHostAuthenticatedPeer,
        request: AdapterRegistrationV1,
    ) -> AdapterRegistrationAcceptedV1:
        plugin = self._plugin_policy(request.plugin_id)
        now = self._clock()
        if peer.host_id not in plugin.allowed_host_ids:
            raise AdapterHostServiceError(
                "authenticated_host_not_allowed",
                f"authenticated host {peer.host_id!r} may not register this plugin",
                http_status=403,
            )
        if request.adapter_instance_id != plugin.adapter_instance_id:
            raise AdapterHostServiceError(
                "adapter_instance_not_allowed",
                "adapter instance id does not match operator-owned configuration",
                http_status=403,
            )
        if request.plugin_api_version != plugin.plugin_api_version:
            raise AdapterHostServiceError(
                "plugin_api_version_mismatch",
                "adapter plugin API version is not supported by this registration",
                http_status=409,
            )
        if request.configuration_digest.lower() != plugin.configuration_digest.lower():
            raise AdapterHostServiceError(
                "configuration_digest_mismatch",
                "adapter configuration does not match operator-owned gateway configuration",
                http_status=403,
            )
        if set(request.channel_names) != set(plugin.channel_names):
            raise AdapterHostServiceError(
                "channel_claim_mismatch",
                "adapter channel claims do not match the operator allowlist",
                http_status=403,
            )
        offered = frozenset(
            AdapterCapabilityV1(value) for value in request.capabilities
        )
        unsupported_claims = offered - plugin.capabilities
        if unsupported_claims:
            raise AdapterHostServiceError(
                "capability_escalation",
                "adapter offered capabilities absent from its trusted descriptor: "
                + ", ".join(sorted(value.value for value in unsupported_claims)),
                http_status=403,
            )
        negotiated = tuple(
            sorted(offered & plugin.capabilities, key=lambda value: value.value)
        )
        if not negotiated:
            raise AdapterHostServiceError(
                "no_negotiated_capabilities",
                "adapter and gateway have no capabilities in common",
                http_status=409,
            )
        limits = self._negotiate_limits(request.supported_limits)
        existing = self._repository.get_registration(
            request.plugin_id, request.adapter_instance_id
        )
        if existing is not None:
            if existing.authenticated_host_id != peer.host_id:
                raise AdapterHostServiceError(
                    "authenticated_host_mismatch",
                    "an existing adapter registration belongs to another authenticated host",
                    http_status=403,
                )
            if existing.registration_id == request.registration_id:
                return self._registration_accepted(
                    request=request,
                    registration=existing,
                )
            if (
                request.resume.inbound_committed_position
                > existing.last_inbound_position
            ):
                raise AdapterHostServiceError(
                    "cursor_ahead_of_gateway",
                    "adapter inbound resume cursor is ahead of gateway durable state",
                )
            existing_stream = self._repository.get_stream(
                request.plugin_id, request.adapter_instance_id
            )
            if (
                request.resume.outbound_received_position
                > existing_stream.next_outbound_position - 1
            ):
                raise AdapterHostServiceError(
                    "cursor_ahead_of_gateway",
                    "adapter outbound resume cursor is beyond the gateway delivery stream",
                )
        stream = self._repository.get_stream(
            request.plugin_id, request.adapter_instance_id
        )
        if (
            request.resume.outbound_completed_position
            < stream.retained_from_position - 1
        ):
            raise AdapterHostServiceError(
                "resume_retention_exhausted",
                "requested resume position predates retained delivery history",
                http_status=409,
                expected_stream_position=stream.retained_from_position,
            )

        epoch = new_id("adapter_epoch")
        lease_expires = now + timedelta(seconds=self._policy.lease_seconds)
        registration = AdapterRegistrationRecord(
            authenticated_host_id=peer.host_id,
            plugin_id=request.plugin_id,
            adapter_instance_id=request.adapter_instance_id,
            registration_id=request.registration_id,
            registration_epoch=epoch,
            configuration_digest=request.configuration_digest.lower(),
            plugin_api_version=request.plugin_api_version,
            implementation_version=request.implementation_version,
            channel_names_json=json.dumps(sorted(request.channel_names)),
            offered_capabilities_json=json.dumps(
                sorted(str(value) for value in request.capabilities)
            ),
            negotiated_capabilities_json=json.dumps(
                [value.value for value in negotiated]
            ),
            negotiated_limits_json=json.dumps(
                limits.model_dump(mode="json"), sort_keys=True
            ),
            lease_expires_at_utc=lease_expires.isoformat(),
            last_heartbeat_at_utc=now.isoformat(),
            state="starting",
            last_inbound_position=(existing.last_inbound_position if existing else 0),
            last_outbound_received_position=(
                existing.last_outbound_received_position if existing else 0
            ),
            last_outbound_completed_position=(
                existing.last_outbound_completed_position if existing else 0
            ),
            reconnect_count=(existing.reconnect_count + 1 if existing else 0),
            error_code=None,
            detail=None,
            created_at_utc=(existing.created_at_utc if existing else now.isoformat()),
            updated_at_utc=now.isoformat(),
        )
        self._repository.save_registration(registration)
        LOGGER.info(
            "Authenticated adapter registered. event=adapter_host_registered "
            "host_id=%s plugin_id=%s adapter_instance_id=%s epoch=%s reconnect_count=%d",
            peer.host_id,
            request.plugin_id,
            request.adapter_instance_id,
            epoch,
            registration.reconnect_count,
        )
        return self._registration_accepted(request=request, registration=registration)

    def _registration_accepted(
        self,
        *,
        request: AdapterRegistrationV1,
        registration: AdapterRegistrationRecord,
    ) -> AdapterRegistrationAcceptedV1:
        return AdapterRegistrationAcceptedV1(
            protocol_version=ADAPTER_HOST_PROTOCOL_VERSION,
            message_type="adapter.registration_accepted.v1",
            registration_id=request.registration_id,
            idempotency_key=request.idempotency_key,
            plugin_id=request.plugin_id,
            adapter_instance_id=request.adapter_instance_id,
            gateway_instance_id=self._policy.gateway_instance_id,
            accepted=True,
            session_epoch=registration.registration_epoch,
            lease_expires_at_utc=datetime.fromisoformat(
                registration.lease_expires_at_utc
            ),
            negotiated_capabilities=tuple(
                AdapterCapabilityV1(value)
                for value in json.loads(registration.negotiated_capabilities_json)
            ),
            negotiated_limits=AdapterProtocolLimitsV1.model_validate_json(
                registration.negotiated_limits_json
            ),
            resume=AdapterResumePositionV1(
                inbound_committed_position=registration.last_inbound_position,
                outbound_received_position=registration.last_outbound_received_position,
                outbound_completed_position=registration.last_outbound_completed_position,
            ),
            accepted_at_utc=self._clock(),
        )

    def _negotiate_limits(
        self, offered: AdapterProtocolLimitsV1
    ) -> AdapterProtocolLimitsV1:
        gateway = self._policy.limits
        return AdapterProtocolLimitsV1(
            max_in_flight_events=min(
                gateway.max_in_flight_events, offered.max_in_flight_events
            ),
            max_payload_bytes=min(gateway.max_payload_bytes, offered.max_payload_bytes),
            max_attachment_bytes=min(
                gateway.max_attachment_bytes, offered.max_attachment_bytes
            ),
            max_pending_events=min(
                gateway.max_pending_events, offered.max_pending_events
            ),
            heartbeat_interval_seconds=min(
                gateway.heartbeat_interval_seconds,
                offered.heartbeat_interval_seconds,
            ),
            acknowledgement_timeout_seconds=min(
                gateway.acknowledgement_timeout_seconds,
                offered.acknowledgement_timeout_seconds,
            ),
            retention_seconds=min(gateway.retention_seconds, offered.retention_seconds),
        )

    def accept_request(
        self,
        peer: AdapterHostAuthenticatedPeer,
        message: ChannelInboundMessageV1 | ChannelCommandV1,
    ) -> ChannelRequestAcceptedV1:
        registration = self._current_registration(
            peer=peer,
            plugin_id=message.plugin_id,
            adapter_instance_id=message.adapter_instance_id,
            session_epoch=message.session_epoch,
        )
        capabilities = set(json.loads(registration.negotiated_capabilities_json))
        required_capability = (
            AdapterCapabilityV1.INBOUND_TEXT.value
            if isinstance(message, ChannelInboundMessageV1)
            else AdapterCapabilityV1.NATIVE_COMMANDS.value
        )
        if required_capability not in capabilities:
            raise AdapterHostServiceError(
                "capability_not_negotiated",
                f"request requires negotiated capability {required_capability!r}",
                http_status=403,
            )
        existing = self._repository.get_inbox_by_idempotency(
            authenticated_host_id=peer.host_id,
            plugin_id=message.plugin_id,
            adapter_instance_id=message.adapter_instance_id,
            registration_epoch=message.session_epoch,
            idempotency_key=message.idempotency_key,
        )
        if existing is not None:
            accepted = ChannelRequestAcceptedV1.model_validate_json(
                existing.admission_json
            )
            return accepted.model_copy(
                update={"status": ChannelRequestStatusV1.DUPLICATE}
            )
        now = self._clock()
        correlation_id = new_id("adapter_request")
        accepted = ChannelRequestAcceptedV1(
            protocol_version=ADAPTER_HOST_PROTOCOL_VERSION,
            message_type="channel.request_accepted.v1",
            request_id=message.request_id,
            idempotency_key=message.idempotency_key,
            status=ChannelRequestStatusV1.ACCEPTED,
            correlation_id=correlation_id,
            accepted_at_utc=now,
        )
        record = AdapterInboxRecord(
            authenticated_host_id=peer.host_id,
            plugin_id=message.plugin_id,
            adapter_instance_id=message.adapter_instance_id,
            registration_epoch=message.session_epoch,
            idempotency_key=message.idempotency_key,
            request_id=message.request_id,
            stream_position=message.stream_position,
            message_type=message.message_type,
            payload_json=json.dumps(
                encode_adapter_host_message_v1(message), sort_keys=True
            ),
            correlation_id=correlation_id,
            admission_json=accepted.model_dump_json(exclude_none=True),
            status="accepted",
            result_json=None,
            error_code=None,
            detail=None,
            created_at_utc=now.isoformat(),
            updated_at_utc=now.isoformat(),
        )
        try:
            stored = self._repository.admit_inbox(
                record,
                expected_previous_position=registration.last_inbound_position,
            )
        except AdapterHostRepositoryConflict as exc:
            raise self._map_repository_conflict(exc) from exc
        if stored.request_id != record.request_id:
            duplicate = ChannelRequestAcceptedV1.model_validate_json(
                stored.admission_json
            )
            return duplicate.model_copy(
                update={"status": ChannelRequestStatusV1.DUPLICATE}
            )
        self._schedule_processing(stored)
        return accepted

    def _schedule_processing(self, record: AdapterInboxRecord) -> None:
        if self._request_handler is None:
            return
        task = asyncio.create_task(
            self._process_record(record),
            name=f"adapter_host_request_{record.request_id}",
        )
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _process_record(self, record: AdapterInboxRecord) -> None:
        handler = self._request_handler
        if handler is None:  # pragma: no cover - scheduling invariant
            return
        if not self._repository.mark_inbox_processing(record.request_id):
            return
        try:
            decoded = decode_adapter_host_message_v1(json.loads(record.payload_json))
            if isinstance(decoded, ChannelInboundMessageV1):
                deliveries = await handler.handle_inbound(decoded)
            elif isinstance(decoded, ChannelCommandV1):
                deliveries = await handler.handle_command(decoded)
            else:  # pragma: no cover - repository corruption guard
                raise TypeError(
                    f"Unsupported admitted adapter request {type(decoded).__name__}."
                )
            for delivery in deliveries:
                await self.enqueue_event(delivery)
            self._repository.complete_inbox(
                record.request_id,
                status="completed",
                result_json=json.dumps(
                    {"delivery_count": len(deliveries)}, sort_keys=True
                ),
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self._repository.complete_inbox(
                record.request_id,
                status="failed",
                error_code="gateway_request_failed",
                detail=str(exc)[:2_000],
            )
            LOGGER.exception(
                "Adapter-host request failed after durable admission. "
                "event=adapter_host_request_failed plugin_id=%s adapter_instance_id=%s "
                "request_id=%s correlation_id=%s",
                record.plugin_id,
                record.adapter_instance_id,
                record.request_id,
                record.correlation_id,
            )

    async def enqueue_event(
        self, event: GatewayDeliveryEventV1 | GatewayRequestStateV1
    ) -> int:
        plugin = self._plugin_policy(event.plugin_id)
        if event.adapter_instance_id != plugin.adapter_instance_id:
            raise AdapterHostServiceError(
                "adapter_instance_not_allowed",
                "delivery targets an adapter instance absent from operator configuration",
                http_status=403,
            )
        limits = self._policy.limits
        registration = self._repository.get_registration(
            event.plugin_id, event.adapter_instance_id
        )
        if registration is not None:
            limits = AdapterProtocolLimitsV1.model_validate_json(
                registration.negotiated_limits_json
            )
        logical_event = event.model_copy(
            update={"session_epoch": "pending", "stream_position": 1}
        )
        payload_json = json.dumps(
            encode_adapter_host_message_v1(logical_event), sort_keys=True
        )
        if len(payload_json.encode("utf-8")) > limits.max_payload_bytes:
            raise AdapterHostServiceError(
                "payload_too_large",
                "delivery exceeds the negotiated maximum payload size",
                http_status=413,
            )
        try:
            record = self._repository.enqueue_outbox(
                event_id=event.event_id,
                idempotency_key=event.idempotency_key,
                plugin_id=event.plugin_id,
                adapter_instance_id=event.adapter_instance_id,
                payload_json=payload_json,
                max_pending_events=limits.max_pending_events,
            )
        except AdapterHostRepositoryConflict as exc:
            raise self._map_repository_conflict(exc) from exc
        async with self._event_condition:
            self._event_condition.notify_all()
        return record.stream_position

    def list_events(
        self,
        peer: AdapterHostAuthenticatedPeer,
        *,
        plugin_id: str,
        adapter_instance_id: str,
        session_epoch: str,
        after_position: int,
        limit: int,
    ) -> tuple[GatewayDeliveryEventV1 | GatewayRequestStateV1, ...]:
        registration = self._current_registration(
            peer=peer,
            plugin_id=plugin_id,
            adapter_instance_id=adapter_instance_id,
            session_epoch=session_epoch,
        )
        stream = self._repository.get_stream(plugin_id, adapter_instance_id)
        if after_position < stream.retained_from_position - 1:
            raise AdapterHostServiceError(
                "resume_retention_exhausted",
                "requested delivery cursor predates retained history",
                expected_stream_position=stream.retained_from_position,
            )
        limits = AdapterProtocolLimitsV1.model_validate_json(
            registration.negotiated_limits_json
        )
        in_flight = max(
            0,
            registration.last_outbound_received_position
            - registration.last_outbound_completed_position,
        )
        available_credit = max(0, limits.max_in_flight_events - in_flight)
        bounded_limit = min(max(0, limit), available_credit)
        if bounded_limit == 0:
            return ()
        records = self._repository.list_outbox_for_delivery(
            plugin_id=plugin_id,
            adapter_instance_id=adapter_instance_id,
            after_position=after_position,
            limit=bounded_limit,
        )
        result: list[GatewayDeliveryEventV1 | GatewayRequestStateV1] = []
        for record in records:
            decoded = decode_adapter_host_message_v1(json.loads(record.payload_json))
            if not isinstance(decoded, (GatewayDeliveryEventV1, GatewayRequestStateV1)):
                raise AdapterHostServiceError(
                    "outbox_corruption",
                    "gateway adapter outbox contains a non-delivery message",
                    http_status=500,
                )
            result.append(
                decoded.model_copy(
                    update={
                        "session_epoch": session_epoch,
                        "stream_position": record.stream_position,
                    }
                )
            )
        return tuple(result)

    async def wait_for_events(self, timeout_seconds: float) -> None:
        try:
            async with self._event_condition:
                await asyncio.wait_for(
                    self._event_condition.wait(), timeout=max(0.01, timeout_seconds)
                )
        except TimeoutError:
            return

    def acknowledge(
        self,
        peer: AdapterHostAuthenticatedPeer,
        acknowledgement: AdapterDeliveryAcknowledgementV1,
    ) -> None:
        self._current_registration(
            peer=peer,
            plugin_id=acknowledgement.plugin_id,
            adapter_instance_id=acknowledgement.adapter_instance_id,
            session_epoch=acknowledgement.session_epoch,
        )
        try:
            self._repository.acknowledge_outbox(
                plugin_id=acknowledgement.plugin_id,
                adapter_instance_id=acknowledgement.adapter_instance_id,
                registration_epoch=acknowledgement.session_epoch,
                event_id=acknowledgement.event_id,
                stream_position=acknowledgement.stream_position,
                stage=str(acknowledgement.stage),
                outcome=(
                    str(acknowledgement.outcome)
                    if acknowledgement.outcome is not None
                    else None
                ),
                provider_message_id=acknowledgement.provider_message_id,
                error_code=acknowledgement.error_code,
                detail=acknowledgement.detail,
                observed_at_utc=acknowledgement.observed_at_utc.isoformat(),
            )
        except AdapterHostRepositoryConflict as exc:
            raise self._map_repository_conflict(exc) from exc

    def heartbeat(
        self,
        peer: AdapterHostAuthenticatedPeer,
        heartbeat: AdapterHeartbeatV1,
    ) -> AdapterRegistrationRecord:
        self._current_registration(
            peer=peer,
            plugin_id=heartbeat.plugin_id,
            adapter_instance_id=heartbeat.adapter_instance_id,
            session_epoch=heartbeat.session_epoch,
            require_live_lease=False,
        )
        lease_expires = self._clock() + timedelta(seconds=self._policy.lease_seconds)
        try:
            return self._repository.update_heartbeat(
                authenticated_host_id=peer.host_id,
                plugin_id=heartbeat.plugin_id,
                adapter_instance_id=heartbeat.adapter_instance_id,
                registration_epoch=heartbeat.session_epoch,
                state=str(heartbeat.state),
                lease_expires_at_utc=lease_expires.isoformat(),
                observed_at_utc=heartbeat.observed_at_utc.isoformat(),
                inbound_committed_position=heartbeat.inbound_committed_position,
                outbound_received_position=heartbeat.outbound_received_position,
                outbound_completed_position=heartbeat.outbound_completed_position,
                error_code=heartbeat.error_code,
                detail=heartbeat.detail,
            )
        except AdapterHostRepositoryConflict as exc:
            raise self._map_repository_conflict(exc) from exc

    def create_upload_ticket(
        self,
        peer: AdapterHostAuthenticatedPeer,
        request: AdapterAttachmentUploadRequestV1,
    ) -> AdapterAttachmentUploadAcceptedV1:
        registration = self._current_registration(
            peer=peer,
            plugin_id=request.plugin_id,
            adapter_instance_id=request.adapter_instance_id,
            session_epoch=request.session_epoch,
        )
        capabilities = set(json.loads(registration.negotiated_capabilities_json))
        if AdapterCapabilityV1.ATTACHMENTS.value not in capabilities:
            raise AdapterHostServiceError(
                "capability_not_negotiated",
                "attachment upload requires the attachments capability",
                http_status=403,
            )
        limits = AdapterProtocolLimitsV1.model_validate_json(
            registration.negotiated_limits_json
        )
        if request.size_bytes > limits.max_attachment_bytes:
            raise AdapterHostServiceError(
                "attachment_too_large",
                "attachment exceeds the negotiated maximum size",
                http_status=413,
            )
        now = self._clock()
        upload_id = new_id("adapter_upload")
        upload_dir = self._policy.upload_dir.expanduser().resolve()
        upload_dir.mkdir(parents=True, exist_ok=True)
        local_path = upload_dir / upload_id
        record = self._repository.save_upload(
            AdapterUploadRecord(
                upload_id=upload_id,
                upload_request_id=request.upload_request_id,
                idempotency_key=request.idempotency_key,
                authenticated_host_id=peer.host_id,
                plugin_id=request.plugin_id,
                adapter_instance_id=request.adapter_instance_id,
                registration_epoch=request.session_epoch,
                filename=request.filename,
                content_type=request.content_type,
                kind=request.kind,
                size_bytes=request.size_bytes,
                sha256=request.sha256.lower(),
                storage_reference=f"adapter-upload:{upload_id}",
                local_path=str(local_path),
                status="pending",
                expires_at_utc=(
                    now + timedelta(seconds=self._policy.upload_ticket_ttl_seconds)
                ).isoformat(),
                created_at_utc=now.isoformat(),
                completed_at_utc=None,
            )
        )
        return AdapterAttachmentUploadAcceptedV1(
            protocol_version=ADAPTER_HOST_PROTOCOL_VERSION,
            message_type="adapter.attachment_upload_accepted.v1",
            upload_request_id=record.upload_request_id,
            idempotency_key=record.idempotency_key,
            plugin_id=record.plugin_id,
            adapter_instance_id=record.adapter_instance_id,
            session_epoch=record.registration_epoch,
            upload_id=record.upload_id,
            upload_path=f"/v1/adapter-host/uploads/{record.upload_id}",
            storage_reference=record.storage_reference,
            expires_at_utc=datetime.fromisoformat(record.expires_at_utc),
            max_bytes=record.size_bytes,
        )

    def accept_upload_bytes(
        self,
        peer: AdapterHostAuthenticatedPeer,
        *,
        upload_id: str,
        payload: bytes,
    ) -> AdapterUploadRecord:
        record = self._repository.get_upload(upload_id)
        if record is None:
            raise AdapterHostServiceError(
                "upload_not_found", "upload ticket does not exist", http_status=404
            )
        self._current_registration(
            peer=peer,
            plugin_id=record.plugin_id,
            adapter_instance_id=record.adapter_instance_id,
            session_epoch=record.registration_epoch,
        )
        if record.authenticated_host_id != peer.host_id:
            raise AdapterHostServiceError(
                "authenticated_host_mismatch",
                "upload ticket belongs to a different authenticated host",
                http_status=403,
            )
        if record.status != "pending" or self._clock() >= datetime.fromisoformat(
            record.expires_at_utc
        ):
            raise AdapterHostServiceError(
                "upload_not_pending",
                "upload ticket is expired or already consumed",
                http_status=409,
            )
        if len(payload) != record.size_bytes:
            raise AdapterHostServiceError(
                "attachment_size_mismatch",
                "uploaded attachment size does not match the ticket",
                http_status=400,
            )
        digest = hashlib.sha256(payload).hexdigest()
        if digest != record.sha256:
            raise AdapterHostServiceError(
                "attachment_digest_mismatch",
                "uploaded attachment digest does not match the ticket",
                http_status=400,
            )
        path = Path(record.local_path)
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        descriptor = os.open(path, flags, 0o600)
        try:
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
        except Exception:
            path.unlink(missing_ok=True)
            raise
        completed_at = self._clock().isoformat()
        try:
            self._repository.complete_upload(upload_id, completed_at_utc=completed_at)
        except Exception:
            path.unlink(missing_ok=True)
            raise
        completed = self._repository.get_upload(upload_id)
        if completed is None:  # pragma: no cover - repository invariant
            raise RuntimeError("completed adapter upload disappeared")
        return completed

    def resolve_upload_reference(
        self,
        *,
        plugin_id: str,
        adapter_instance_id: str,
        session_epoch: str,
        storage_reference: str,
    ) -> AdapterUploadRecord:
        scheme, _, upload_id = storage_reference.partition(":")
        if scheme != "adapter-upload" or not upload_id:
            raise AdapterHostServiceError(
                "invalid_attachment_reference",
                "only adapter-upload references resolve through this operation",
                http_status=400,
            )
        record = self._repository.get_upload(upload_id)
        if (
            record is None
            or record.status != "completed"
            or record.plugin_id != plugin_id
            or record.adapter_instance_id != adapter_instance_id
            or record.registration_epoch != session_epoch
        ):
            raise AdapterHostServiceError(
                "attachment_reference_not_found",
                "attachment reference is missing or belongs to another adapter registration",
                http_status=404,
            )
        return record

    def stage_outbound_attachment(
        self,
        *,
        plugin_id: str,
        adapter_instance_id: str,
        payload: bytes,
        filename: str,
        content_type: str,
        kind: str,
    ) -> AdapterAttachmentV1:
        """Store one attachment's bytes in the opaque, bounded transfer store.

        The caller passes bytes the gateway already checked against the
        staged copy's SHA-256 (``assistant_gateway.attachments``), never a
        path, so nothing here reads a file an agent's code could change.
        """

        plugin = self._plugin_policy(plugin_id)
        if adapter_instance_id != plugin.adapter_instance_id:
            raise AdapterHostServiceError(
                "adapter_instance_not_allowed",
                "attachment targets an adapter instance absent from operator configuration",
                http_status=403,
            )
        registration = self._repository.get_registration(plugin_id, adapter_instance_id)
        limits = (
            AdapterProtocolLimitsV1.model_validate_json(
                registration.negotiated_limits_json
            )
            if registration is not None
            else self._policy.limits
        )
        size_bytes = len(payload)
        if size_bytes > limits.max_attachment_bytes:
            raise AdapterHostServiceError(
                "attachment_too_large",
                "outbound attachment exceeds the negotiated maximum size",
                http_status=413,
            )
        digest = hashlib.sha256(payload).hexdigest()
        object_id = new_id("adapter_object")
        transfer_dir = self._policy.upload_dir.expanduser().resolve()
        transfer_dir.mkdir(parents=True, exist_ok=True)
        target = transfer_dir / object_id
        descriptor = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
        except Exception:
            target.unlink(missing_ok=True)
            raise
        now = self._clock()
        record = AdapterUploadRecord(
            upload_id=object_id,
            upload_request_id=object_id,
            idempotency_key=f"gateway-object:{digest}:{object_id}",
            authenticated_host_id=(
                registration.authenticated_host_id
                if registration is not None
                else sorted(plugin.allowed_host_ids)[0]
            ),
            plugin_id=plugin_id,
            adapter_instance_id=adapter_instance_id,
            registration_epoch=(
                registration.registration_epoch
                if registration is not None
                else "pending"
            ),
            filename=Path(filename or "attachment").name,
            content_type=content_type or "application/octet-stream",
            kind=kind or "other",
            size_bytes=size_bytes,
            sha256=digest,
            storage_reference=f"gateway-object:{object_id}",
            local_path=str(target),
            status="completed",
            expires_at_utc=(
                now + timedelta(seconds=limits.retention_seconds)
            ).isoformat(),
            created_at_utc=now.isoformat(),
            completed_at_utc=now.isoformat(),
        )
        try:
            self._repository.save_upload(record)
        except Exception:
            target.unlink(missing_ok=True)
            raise
        return AdapterAttachmentV1(
            attachment_id=object_id,
            filename=record.filename,
            content_type=record.content_type,
            kind=record.kind,
            size_bytes=record.size_bytes,
            sha256=record.sha256,
            storage_reference=record.storage_reference,
        )

    def read_outbound_attachment(
        self,
        peer: AdapterHostAuthenticatedPeer,
        *,
        plugin_id: str,
        adapter_instance_id: str,
        session_epoch: str,
        object_id: str,
    ) -> tuple[AdapterUploadRecord, bytes]:
        self._current_registration(
            peer=peer,
            plugin_id=plugin_id,
            adapter_instance_id=adapter_instance_id,
            session_epoch=session_epoch,
        )
        record = self._repository.get_upload(object_id)
        if (
            record is None
            or record.status != "completed"
            or record.storage_reference != f"gateway-object:{object_id}"
            or record.plugin_id != plugin_id
            or record.adapter_instance_id != adapter_instance_id
        ):
            raise AdapterHostServiceError(
                "attachment_reference_not_found",
                "outbound attachment reference is missing or belongs to another adapter",
                http_status=404,
            )
        path = Path(record.local_path)
        payload = path.read_bytes()
        if (
            len(payload) != record.size_bytes
            or hashlib.sha256(payload).hexdigest() != record.sha256
        ):
            raise AdapterHostServiceError(
                "attachment_transfer_corrupt",
                "stored outbound attachment failed its size or digest check",
                http_status=500,
            )
        return record, payload

    def health(self, plugin_id: str, adapter_instance_id: str) -> dict[str, object]:
        snapshot = self._repository.health_snapshot(plugin_id, adapter_instance_id)
        if snapshot is None:
            return {
                "liveness": "unknown",
                "readiness": "not_registered",
                "degraded": False,
                "plugin_id": plugin_id,
                "adapter_instance_id": adapter_instance_id,
            }
        lease_valid = self._clock() < datetime.fromisoformat(
            snapshot.lease_expires_at_utc
        )
        limits = AdapterProtocolLimitsV1.model_validate_json(
            self._current_registration_limits(snapshot)
        )
        oldest_pending_age_seconds: float | None = None
        acknowledgement_overdue = False
        if snapshot.oldest_pending_at_utc is not None:
            oldest_pending_age_seconds = max(
                0.0,
                (
                    self._clock()
                    - datetime.fromisoformat(snapshot.oldest_pending_at_utc)
                ).total_seconds(),
            )
            acknowledgement_overdue = (
                oldest_pending_age_seconds > limits.acknowledgement_timeout_seconds
            )
        degraded = bool(
            snapshot.error_code
            or snapshot.state == "degraded"
            or snapshot.pending_event_count >= limits.max_pending_events
            or acknowledgement_overdue
        )
        return {
            "liveness": "live" if lease_valid else "expired",
            "readiness": (
                "ready"
                if lease_valid and snapshot.state == "ready" and not degraded
                else "not_ready"
            ),
            "degraded": degraded,
            "acknowledgement_overdue": acknowledgement_overdue,
            "oldest_pending_age_seconds": oldest_pending_age_seconds,
            **snapshot.__dict__,
        }

    def _current_registration_limits(self, snapshot: AdapterHostHealthSnapshot) -> str:
        registration = self._repository.get_registration(
            snapshot.plugin_id, snapshot.adapter_instance_id
        )
        if registration is None:  # pragma: no cover - snapshot/repository invariant
            return self._policy.limits.model_dump_json()
        return registration.negotiated_limits_json

    @staticmethod
    def _map_repository_conflict(
        error: AdapterHostRepositoryConflict,
    ) -> AdapterHostServiceError:
        status = 429 if error.error_code == "outbox_backpressure" else 409
        return AdapterHostServiceError(
            error.error_code,
            error.detail,
            http_status=status,
            retryable=error.error_code == "outbox_backpressure",
            expected_stream_position=error.expected_stream_position,
        )


__all__ = [
    "AdapterHostApplicationService",
    "AdapterHostAuthenticatedPeer",
    "AdapterHostPolicy",
    "AdapterHostRequestHandler",
    "AdapterHostServiceError",
    "AdapterPluginPolicy",
    "canonical_configuration_digest",
]
