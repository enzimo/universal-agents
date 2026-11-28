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
    check_inbound_attachment_bytes,
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

    @property
    def max_file_bytes(self) -> int:
        """Normalized byte limit matching ``check_bytes`` and ``store_bytes``."""
        ...

    def check_bytes(self, *, data: bytes, filename: str, content_type: str) -> None:
        """Refuse an upload ``store_bytes`` would refuse, without saving anything."""
        ...

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

    def stage_bytes(
        self,
        *,
        data: bytes,
        filename: str,
        content_type: str,
        session_key: str,
        origin: AttachmentOrigin,
        caption: str | None = None,
    ) -> StoredInboundAttachment:
        """Save bytes like ``store_bytes`` but leave them unregistered.

        For all-or-nothing batches that should not hold every file in memory:
        save each checked file as it arrives, then ``register_staged`` each one
        once the whole batch passed, or ``discard_staged`` them all when a later
        file is refused or fails.
        """
        ...

    def register_staged(
        self,
        stored: StoredInboundAttachment,
        *,
        session_key: str,
        conversation_id: str | None,
    ) -> None:
        """Register a file ``stage_bytes`` saved, as ``store_bytes`` would."""
        ...

    def discard_staged(self, stored: StoredInboundAttachment) -> None:
        """Delete a file ``stage_bytes`` saved and that was never registered."""
        ...


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

    @property
    def max_file_bytes(self) -> int:
        return max(1, int(self._policy.max_file_bytes))

    def check_bytes(self, *, data: bytes, filename: str, content_type: str) -> None:
        check_inbound_attachment_bytes(
            data=data, filename=filename, content_type=content_type, policy=self._policy
        )

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
        stored = self.stage_bytes(
            data=data,
            filename=filename,
            content_type=content_type,
            session_key=session_key,
            origin=origin,
            caption=caption,
        )
        self.register_staged(
            stored, session_key=session_key, conversation_id=conversation_id
        )
        return stored

    def stage_bytes(
        self,
        *,
        data: bytes,
        filename: str,
        content_type: str,
        session_key: str,
        origin: AttachmentOrigin,
        caption: str | None = None,
    ) -> StoredInboundAttachment:
        return store_inbound_attachment_bytes_with_policy(
            data=data,
            filename=filename,
            content_type=content_type,
            policy=self._policy,
            session_key=session_key,
            conversation_id=None,
            origin=origin,
            caption=caption,
        )

    def register_staged(
        self,
        stored: StoredInboundAttachment,
        *,
        session_key: str,
        conversation_id: str | None,
    ) -> None:
        if self._register_with_application:
            self._application.register_inbound_attachment(
                stored.attachment,
                session_key=session_key,
                conversation_id=conversation_id,
                expires_at=stored.expires_at,
            )

    def discard_staged(self, stored: StoredInboundAttachment) -> None:
        stored.local_path.unlink(missing_ok=True)


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
    # Where people's browsers reach the secure broker's pages; Web Chat frames them (website_support.web_chat_csp).
    broker_public_base_url: str
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
