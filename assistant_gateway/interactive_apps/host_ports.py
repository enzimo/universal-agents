"""Explicit host services supplied to in-process and external adapter plugins."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from runtime_settings.voice import VoiceSettings
from assistant_gateway.attachments import StagedAttachmentReader
from assistant_gateway.inbound_attachments import (
    InboundAttachmentStoragePolicy,
    StoredInboundAttachment,
    store_inbound_attachment_bytes_with_policy,
)
from assistant_gateway.interactive_apps.contracts import InteractiveAppApplication
from assistant_gateway.voice import VoiceClient
from custom_messages.contracts.authorization_presentation_mode import (
    AuthorizationPresentationMode,
)
from custom_messages.contracts.attachments_v1 import AttachmentOrigin


class InteractiveAppAttachmentStore(Protocol):
    """Validate/store bytes without exposing a repository or aggregate config."""

    def store_bytes(
        self,
        *,
        data: bytes,
        filename: str,
        content_type: str,
        session_key: str,
        conversation_id: str | None,
        origin: AttachmentOrigin,
        caption: str | None = None,
    ) -> StoredInboundAttachment: ...


class LocalInteractiveAppAttachmentStore:
    """Gateway-hosted attachment port with explicit policy and application write."""

    def __init__(
        self,
        policy: InboundAttachmentStoragePolicy,
        application: InteractiveAppApplication,
        *,
        register_with_application: bool = True,
    ) -> None:
        self._policy = policy
        self._application = application
        self._register_with_application = register_with_application

    def store_bytes(
        self,
        *,
        data: bytes,
        filename: str,
        content_type: str,
        session_key: str,
        conversation_id: str | None,
        origin: AttachmentOrigin,
        caption: str | None = None,
    ) -> StoredInboundAttachment:
        stored = store_inbound_attachment_bytes_with_policy(
            data=data,
            filename=filename,
            content_type=content_type,
            policy=self._policy,
            session_key=session_key,
            conversation_id=conversation_id,
            origin=origin,
            caption=caption,
        )
        if self._register_with_application:
            self._application.register_inbound_attachment(
                stored.attachment,
                session_key=session_key,
                conversation_id=conversation_id,
                expires_at=stored.expires_at,
            )
        return stored


@dataclass(frozen=True)
class InteractiveAppRuntimeContext:
    """Non-secret scalar settings and explicit host ports shared by plugins."""

    request_timeout_seconds: float
    voice: VoiceSettings
    voice_client: VoiceClient | None
    attachments: InteractiveAppAttachmentStore
    # Files the gateway staged for people: channels send only what this reads
    # (links and changed bytes are refused, assistant_gateway.attachments).
    outgoing_attachments: StagedAttachmentReader
    authorization_presentation_mode: AuthorizationPresentationMode = (
        AuthorizationPresentationMode.FRIENDLY
    )
    website: InternalWebsiteHostEnvironment | None = None


@dataclass(frozen=True)
class InternalWebsiteHostEnvironment:
    """Explicit gateway-owned values needed by the local operator website."""

    instance_root_path: Path
    workspace_path: Path
    config_path: Path
    base_url: str
    public_origin: str
    assistant_display_name: str
    appearance: dict[str, object]
    activity_collector_base_url: str
    activity_collector_admin_token: str | None
    activity_collector_request_timeout_seconds: float
    agent_debug: dict[str, object]


__all__ = [
    "InteractiveAppAttachmentStore",
    "InteractiveAppRuntimeContext",
    "InternalWebsiteHostEnvironment",
    "LocalInteractiveAppAttachmentStore",
]
