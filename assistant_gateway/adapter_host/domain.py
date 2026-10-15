"""Gateway-owned durable records for the adapter-host boundary."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class AdapterRegistrationRecord:
    authenticated_host_id: str
    plugin_id: str
    adapter_instance_id: str
    registration_id: str
    registration_epoch: str
    configuration_digest: str
    plugin_api_version: str
    implementation_version: str
    channel_names_json: str
    offered_capabilities_json: str
    negotiated_capabilities_json: str
    negotiated_limits_json: str
    lease_expires_at_utc: str
    last_heartbeat_at_utc: str
    state: str
    last_inbound_position: int
    last_outbound_received_position: int
    last_outbound_completed_position: int
    reconnect_count: int
    error_code: str | None
    detail: str | None
    created_at_utc: str
    updated_at_utc: str


@dataclass(frozen=True)
class AdapterInboxRecord:
    authenticated_host_id: str
    plugin_id: str
    adapter_instance_id: str
    registration_epoch: str
    idempotency_key: str
    request_id: str
    stream_position: int
    message_type: str
    payload_json: str
    correlation_id: str
    admission_json: str
    status: str
    result_json: str | None
    error_code: str | None
    detail: str | None
    created_at_utc: str
    updated_at_utc: str


@dataclass(frozen=True)
class AdapterOutboxRecord:
    event_id: str
    idempotency_key: str
    plugin_id: str
    adapter_instance_id: str
    stream_position: int
    payload_json: str
    status: str
    received_epoch: str | None
    completed_epoch: str | None
    outcome: str | None
    provider_message_id: str | None
    attempt_count: int
    error_code: str | None
    detail: str | None
    created_at_utc: str
    received_at_utc: str | None
    completed_at_utc: str | None
    updated_at_utc: str


@dataclass(frozen=True)
class AdapterUploadRecord:
    upload_id: str
    upload_request_id: str
    idempotency_key: str
    authenticated_host_id: str
    plugin_id: str
    adapter_instance_id: str
    registration_epoch: str
    filename: str
    content_type: str
    kind: str
    size_bytes: int
    sha256: str
    storage_reference: str
    local_path: str
    status: str
    expires_at_utc: str
    created_at_utc: str
    completed_at_utc: str | None


@dataclass(frozen=True)
class AdapterStreamRecord:
    plugin_id: str
    adapter_instance_id: str
    next_outbound_position: int
    retained_from_position: int
    updated_at_utc: str


@dataclass(frozen=True)
class AdapterHostHealthSnapshot:
    authenticated_host_id: str
    plugin_id: str
    adapter_instance_id: str
    registration_epoch: str
    state: str
    lease_expires_at_utc: str
    last_heartbeat_at_utc: str
    last_inbound_position: int
    last_outbound_received_position: int
    last_outbound_completed_position: int
    pending_event_count: int
    in_flight_event_count: int
    oldest_pending_at_utc: str | None
    reconnect_count: int
    error_code: str | None
    detail: str | None
