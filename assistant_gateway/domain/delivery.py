"""Gateway delivery-domain persistence records."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class DeliveryRecord:
    delivery_id: str
    correlation_id: str | None
    session_key: str
    origin_kind: str
    payload_json: str
    rendered_text: str
    available_after: str
    status: str
    created_at: str
    delivered_at: str | None
    owner_canonical_user_id: str | None = None


@dataclass
class AttachmentRecord:
    attachment_id: str
    session_key: str
    conversation_id: str | None
    correlation_id: str | None
    filename: str
    content_type: str
    kind: str
    size_bytes: int
    sha256: str
    local_path: str
    nats_bucket: str | None
    nats_object_name: str | None
    created_at: str
    expires_at: str | None
    owner_canonical_user_id: str | None = None


@dataclass
class ScheduledTargetRecord:
    scheduled_job_id: str
    session_key: str
    conversation_id: str | None
    created_at: str
    updated_at: str
    owner_canonical_user_id: str | None = None
