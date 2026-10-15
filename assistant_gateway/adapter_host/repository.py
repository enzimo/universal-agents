"""Domain-scoped repository port for durable adapter-host state."""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from assistant_gateway.adapter_host.domain import (
    AdapterHostHealthSnapshot,
    AdapterInboxRecord,
    AdapterOutboxRecord,
    AdapterRegistrationRecord,
    AdapterStreamRecord,
    AdapterUploadRecord,
)


@runtime_checkable
class AdapterHostRepository(Protocol):
    """Persist registrations, ordered inbox/outbox ledgers, and upload tickets."""

    def get_registration(
        self, plugin_id: str, adapter_instance_id: str
    ) -> AdapterRegistrationRecord | None: ...

    def save_registration(self, record: AdapterRegistrationRecord) -> None: ...

    def get_inbox_by_idempotency(
        self,
        *,
        authenticated_host_id: str,
        plugin_id: str,
        adapter_instance_id: str,
        registration_epoch: str,
        idempotency_key: str,
    ) -> AdapterInboxRecord | None: ...

    def admit_inbox(
        self,
        record: AdapterInboxRecord,
        *,
        expected_previous_position: int,
    ) -> AdapterInboxRecord: ...

    def mark_inbox_processing(self, request_id: str) -> bool: ...

    def complete_inbox(
        self,
        request_id: str,
        *,
        status: str,
        result_json: str | None = None,
        error_code: str | None = None,
        detail: str | None = None,
    ) -> None: ...

    def list_inbox_for_recovery(
        self, *, limit: int = 100
    ) -> list[AdapterInboxRecord]: ...

    def mark_interrupted_inbox_outcome_unknown(self) -> int: ...

    def enqueue_outbox(
        self,
        *,
        event_id: str,
        idempotency_key: str,
        plugin_id: str,
        adapter_instance_id: str,
        payload_json: str,
        max_pending_events: int,
    ) -> AdapterOutboxRecord: ...

    def list_outbox_for_delivery(
        self,
        *,
        plugin_id: str,
        adapter_instance_id: str,
        after_position: int,
        limit: int,
    ) -> list[AdapterOutboxRecord]: ...

    def acknowledge_outbox(
        self,
        *,
        plugin_id: str,
        adapter_instance_id: str,
        registration_epoch: str,
        event_id: str,
        stream_position: int,
        stage: str,
        outcome: str | None,
        provider_message_id: str | None,
        error_code: str | None,
        detail: str | None,
        observed_at_utc: str,
    ) -> AdapterOutboxRecord: ...

    def get_stream(
        self, plugin_id: str, adapter_instance_id: str
    ) -> AdapterStreamRecord: ...

    def update_heartbeat(
        self,
        *,
        authenticated_host_id: str,
        plugin_id: str,
        adapter_instance_id: str,
        registration_epoch: str,
        state: str,
        lease_expires_at_utc: str,
        observed_at_utc: str,
        inbound_committed_position: int,
        outbound_received_position: int,
        outbound_completed_position: int,
        error_code: str | None,
        detail: str | None,
    ) -> AdapterRegistrationRecord: ...

    def health_snapshot(
        self, plugin_id: str, adapter_instance_id: str
    ) -> AdapterHostHealthSnapshot | None: ...

    def save_upload(self, record: AdapterUploadRecord) -> AdapterUploadRecord: ...

    def get_upload(self, upload_id: str) -> AdapterUploadRecord | None: ...

    def complete_upload(self, upload_id: str, *, completed_at_utc: str) -> None: ...

    def prune(self, *, cutoff_utc: str) -> dict[str, int]: ...
