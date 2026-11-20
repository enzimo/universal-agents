"""Gateway delivery-domain persistence records."""

from __future__ import annotations

from dataclasses import dataclass

# The id prefix of the gateway's attachment record for a file it sent to a person (``new_transcript_attachment_id``):
# a copy staged in the person's ``scratch/sent`` folder under a name the agent chose. Files people send in get other
# ids. The 0.9 upgrade step for adapter-host files never names such a record's folder (SEC-1 of PR #168's review).
SENT_FILE_ATTACHMENT_ID_PREFIX = "web_attachment"


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
