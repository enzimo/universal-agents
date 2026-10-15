"""attachments v1 contracts, independent of runtime hosts and SDKs."""

from datetime import datetime
from datetime import timezone
from pydantic import BaseModel
from pydantic import Field
from typing import List
from typing import Literal
from typing import Optional


AttachmentKind = Literal["image", "document", "audio", "video", "other"]


AttachmentStorageType = Literal["local", "nats_object"]


AttachmentOrigin = Literal[
    "web_chat", "telegram", "discord", "whatsapp", "signal", "photon", "cli", "other"
]


class AttachmentStorageRef(BaseModel):
    """Structured reference to attachment bytes stored outside NATS messages."""

    storage_type: AttachmentStorageType = Field(
        description="Storage backend for this attachment reference."
    )
    path: Optional[str] = Field(
        default=None, description="Local filesystem path when storage_type is local."
    )
    bucket: Optional[str] = Field(
        default=None,
        description="NATS Object Store bucket when storage_type is nats_object.",
    )
    object_name: Optional[str] = Field(
        default=None,
        description="NATS Object Store object name when storage_type is nats_object.",
    )


class UserTaskAttachment(BaseModel):
    """User-provided attachment metadata for Task and TaskSteering payloads."""

    attachment_id: str = Field(
        description="Stable gateway-generated attachment identifier."
    )
    filename: str = Field(description="Sanitized user-visible filename.")
    content_type: str = Field(
        default="application/octet-stream", description="Resolved MIME content type."
    )
    kind: AttachmentKind = Field(
        default="other", description="High-level attachment kind."
    )
    size_bytes: int = Field(description="Attachment byte size.")
    sha256: str = Field(description="SHA-256 digest of the attachment bytes.")
    created_at: str = Field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )
    origin: AttachmentOrigin = Field(
        default="web_chat", description="Surface that originated this attachment."
    )
    storage_refs: List[AttachmentStorageRef] = Field(default_factory=list)
    caption: Optional[str] = Field(
        default=None, description="Optional user-supplied attachment caption."
    )

    def local_path(self) -> Optional[str]:
        for ref in self.storage_refs:
            if ref.storage_type == "local" and ref.path:
                return ref.path
        return None
