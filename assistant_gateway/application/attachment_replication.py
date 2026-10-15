"""Application-owned replication of validated attachments for remote workers."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from assistant_gateway.application.delivery_service import GatewayDeliveryService
from assistant_gateway.application.manager_client import GatewayManagerClient


@dataclass(frozen=True)
class GatewayAttachmentReplicationSettings:
    enabled: bool
    bucket: str
    ttl_seconds: float
    max_bytes: int | None


@dataclass(frozen=True)
class GatewayAttachmentReplicationResult:
    attachment_id: str
    bucket: str
    object_name: str
    size_bytes: int
    sha256: str
    content_type: str
    updated: bool


class GatewayAttachmentReplicationApplication:
    """Own validated local-to-object-store attachment replication."""

    def __init__(
        self,
        *,
        settings: GatewayAttachmentReplicationSettings,
        delivery: GatewayDeliveryService,
        manager: GatewayManagerClient,
    ) -> None:
        self._settings = settings
        self._delivery = delivery
        self._manager = manager

    async def mirror(
        self,
        attachment_id: str,
    ) -> GatewayAttachmentReplicationResult:
        if not self._settings.enabled:
            raise RuntimeError("Web Chat attachment remote-copy support is disabled.")
        record = self._delivery.get_attachment(attachment_id)
        if record is None:
            raise FileNotFoundError(f"Attachment {attachment_id} was not found.")
        local_path = Path(record.local_path).expanduser().resolve()
        if not local_path.is_file():
            raise FileNotFoundError(
                f"Attachment bytes are not available locally: {local_path}"
            )
        object_name = (
            record.nats_object_name or f"{record.attachment_id}/{record.filename}"
        )
        await self._manager.put_object(
            bucket=self._settings.bucket,
            object_name=object_name,
            path=str(local_path),
            ttl_seconds=self._settings.ttl_seconds,
            max_bytes=self._settings.max_bytes,
            headers={
                "content-type": record.content_type,
                "filename": record.filename,
                "sha256": record.sha256,
                "attachment-id": record.attachment_id,
            },
        )
        updated = self._delivery.update_attachment_nats_ref(
            attachment_id=record.attachment_id,
            nats_bucket=self._settings.bucket,
            nats_object_name=object_name,
        )
        return GatewayAttachmentReplicationResult(
            attachment_id=record.attachment_id,
            bucket=self._settings.bucket,
            object_name=object_name,
            size_bytes=record.size_bytes,
            sha256=record.sha256,
            content_type=record.content_type,
            updated=bool(updated),
        )
